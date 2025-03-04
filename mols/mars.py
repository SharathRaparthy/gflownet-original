import argparse
from copy import copy, deepcopy
from collections import defaultdict
from datetime import timedelta
import gc
import gzip
import os
import os.path as osp
import pickle
import psutil
import pdb
import subprocess
import sys
import threading
import time
import traceback
import warnings
import wandb
import random
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import Descriptors
from rdkit.Chem import QED
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data, Batch
import torch_geometric.nn as gnn
import torch_geometric.data as gd
from torch.distributions import Categorical
import concurrent.futures

from mol_mdp_ext import MolMDPExtended, BlockMoleculeDataExtended
from gflownet import Proxy, make_model
import reward_proxy
from utils import sascore
import model_atom, model_block, model_fingerprint
from compute_metrics import MultiObjectiveStatsHook

parser = argparse.ArgumentParser()

parser.add_argument("--learning_rate", default=2.5e-4, help="Learning rate", type=float)
parser.add_argument("--mbsize", default=32, help="Minibatch size", type=int)
parser.add_argument("--opt_beta", default=0.9, type=float)
parser.add_argument("--opt_beta2", default=0.999, type=float)
parser.add_argument("--nemb", default=32, help="#hidden", type=int)
parser.add_argument("--min_blocks", default=2, type=int)
parser.add_argument("--max_blocks", default=8, type=int)
parser.add_argument("--num_iterations", default=200, type=int)
parser.add_argument("--max_generated_mols", default=1e6, type=int)
parser.add_argument("--num_conv_steps", default=12, type=int)
parser.add_argument("--log_reg_c", default=0, type=float)
parser.add_argument("--reward_exp", default=4, type=float)
parser.add_argument("--seed", default=4, type=int)
parser.add_argument("--sample_prob", default=1, type=float)
parser.add_argument("--clip_grad", default=0, type=float)
parser.add_argument("--clip_loss", default=0, type=float)
parser.add_argument("--buffer_size", default=5000, type=int)
parser.add_argument("--num_sgd_steps", default=25, type=int)
parser.add_argument("--bootstrap_tau", default=0, type=float)
parser.add_argument("--array", default='')
parser.add_argument("--repr_type", default='atom_graph')
parser.add_argument("--floatX", default='float64')
parser.add_argument("--model_version", default='v5')
parser.add_argument("--run", default=0, help="run", type=int)
parser.add_argument("--save_path", default='results/mars/')
parser.add_argument("--proxy_path", default='data/pretrained_proxy/')
parser.add_argument("--print_array_length", default=False, action='store_true')
parser.add_argument("--progress", default='yes')
parser.add_argument("--reward_type", default='sum')
parser.add_argument("--use_wandb", default=False, action='store_true')
parser.add_argument("--num_objectives", default=2, type=int)


class SplitCategorical:
    def __init__(self, n, logits):
        """Two mutually exclusive categoricals, stored in logits[..., :n] and
        logits[..., n:], that have probability 1/2 each."""
        self.cats = Categorical(logits=logits[..., :n]), Categorical(logits=logits[..., n:])
        self.n = n
        self.logits = logits

    def sample(self):
        split = torch.rand(self.logits.shape[:-1]) < 0.5
        return self.cats[0].sample() * split + (self.n + self.cats[1].sample()) * (~split)

    def log_prob(self, a):
        split = a < self.n
        log_one_half = -0.693147
        return (log_one_half +  # We need to multiply the prob by 0.5, so add log(0.5) to logprob
                self.cats[0].log_prob(torch.minimum(a, torch.tensor(self.n - 1).to(a.device))) * split +
                self.cats[1].log_prob(torch.maximum(a - self.n, torch.tensor(0).to(a.device))) * (~split))

    def entropy(self):
        return Categorical(probs=torch.cat([self.cats[0].probs, self.cats[1].probs], -1) * 0.5).entropy()


def _load_task_models(pretrained_path):
    model = reward_proxy.load_original_model(pretrained_path)
    return {'seh': model}


class Dataset:

    def __init__(self, args, bpath, device, repr_type, floatX=torch.double):
        self.mol_buffer = None
        self.test_split_rng = np.random.RandomState(142857)
        self.train_rng = np.random.RandomState(int(time.time()))
        self.train_mols = []
        self.test_mols = []
        self.train_mols_map = {}
        self.mdp = MolMDPExtended(bpath)
        self.mdp.post_init(device, repr_type, include_bonds=True)
        self.mdp.build_translation_table()
        self.floatX = floatX
        self.mdp.floatX = self.floatX
        self._device = device
        self.seen_molecules = set()
        self.stop_event = threading.Event()
        self.target_norm = [-8.6, 1.10]
        self.sampling_model = None
        self.sampling_model_prob = 0
        self.R_min = 1e-8
        self.min_blocks = args.min_blocks
        self.max_blocks = args.max_blocks
        self.floatX = torch.double
        self.mdp.floatX = self.floatX
        self.args = args
        #######
        # This is the "result", here a list of (reward, BlockMolDataExt) tuples
        self.sampled_mols = []
        self.current_mols = []
        self.reward_exp = args.reward_exp
        self.proxy_reward = _load_task_models(args.proxy_path)['seh']
        self.stats_hook = MultiObjectiveStatsHook(256, args.num_objectives)

        if args.use_wandb:
            # Wandb project initialization
            wandb.init(project="MARS baseline", entity="mogfn",
                       name=f"Finalv3 | No. Of Objectives - {args.num_objectives}")
            wandb.config.update(args)

    def set_sampling_model(self, model, sample_prob=0.5):
        self.sampling_model = model
        self.sampling_model_prob = sample_prob
        print("Starting buffer")
        self.mol_buffer = [(m, self._get_reward(m))
                           for i in tqdm(range(self.args.buffer_size))
                           for m in [self.mdp.add_block_to(BlockMoleculeDataExtended(),
                                                           i % self.mdp.num_blocks)]]

    def _step_buffer(self, i):
        m, rew_list = self.mol_buffer[i]
        r, flat_reward = rew_list[0], rew_list[1]
        s = self.mdp.mols2batch([self.mdp.mol2repr(m)])
        with torch.no_grad():
            s_o, m_o, b_o = self.sampling_model(s, do_bonds=True)
        num_stem_acts = np.prod(s_o.shape)
        s_o = s_o.flatten()
        b_o = b_o.flatten()
        if len(m.jbonds):
            # Determine which edges we can actually cut
            blocks_degree = defaultdict(int)
            for a, b, _, _ in m.jbonds:
                blocks_degree[a] += 1
                blocks_degree[b] += 1
            bond_is_degree_1 = torch.tensor([float(blocks_degree[a] == 1 or
                                                   blocks_degree[b] == 1)
                                             for a, b, _, _ in m.jbonds],
                                            device=self._device)
            # unlikely logits for bonds which aren't cuttable
            b_o = b_o * bond_is_degree_1 - 1000 * (1 - bond_is_degree_1)
        else:
            b_o = b_o * 0 - 1000
        if len(m.blocks) >= self.max_blocks or not len(m.stems):
            # We can't add any more blocks to this mol, let's sample a break action
            cat = Categorical(logits=b_o)
            action = cat.sample().item() + num_stem_acts
        elif len(m.jbonds) < 1:
            # We can't break a bond, let's sample an add action
            cat = Categorical(logits=s_o)
            action = cat.sample().item()
        else:
            logits = torch.cat([s_o, b_o])
            cat = SplitCategorical(num_stem_acts, logits=logits)
            action = cat.sample().item()

        if action < num_stem_acts:
            m_new = self.mdp.add_block_to(m, action % self.mdp.num_blocks,
                                          action // self.mdp.num_blocks)
            # reverse_action = len(m_new.stems) * self.mdp.num_blocks + len(m_new.jbonds) - 1
            # m_back = self.mdp.remove_jbond_from(m_new, len(m_new.jbonds)-1)
            # print(m.blockidxs, m_new.blockidxs, m_back.blockidxs)
        else:
            action = action - num_stem_acts
            m_new = self.mdp.remove_jbond_from(m, action)
            # reverse_action = m.jbonds[action]
        r_new, flat_reward_new = self._get_reward(m_new)

        A = r_new / r  # should include reverse action prob... but the paper says no
        U = self.train_rng.uniform()
        if A > U:
            self.mol_buffer[i] = m_new, (r_new, flat_reward_new)
            self.sampled_mols.append((r_new, m_new, flat_reward_new))
            self.current_mols.append((r_new, m_new, flat_reward_new))
        if r_new > r:
            self.train_mols.append((m, action))

    def step_all(self, n):
        if n == 1:
            for i in range(len(self.mol_buffer)):
                self._step_buffer(i)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as executor:
                futures = [executor.submit(self._step_buffer, i)
                           for i in range(len(self.mol_buffer))]
                for future in tqdm(concurrent.futures.as_completed(futures), leave=False):
                    pass

    def _get_reward(self, m):
        rdmol = m.mol
        graph = [reward_proxy.mol2graph(rdmol)]
        is_valid = torch.tensor([i is not None for i in graph]).bool()
        if not is_valid.any():
            invalid_rewards = -1 * np.ones(self.args.num_objectives)
            if self.args.reward_type == "sum":
                return invalid_rewards.sum(), invalid_rewards
            else:
                return invalid_rewards.prod(), invalid_rewards
        batch = gd.Batch.from_data_list([i for i in graph if i is not None])
        seh_preds = self.proxy_reward(batch).reshape(-1).clip(1e-4, 100).data.cpu() / 8
        seh_preds[seh_preds.isnan()] = 0

        def safe(f, x, default):
            try:
                return f(x)
            except Exception:
                return default

        def clamp(num, min_value, max_value):
            return max(min(num, max_value), min_value)

        qeds = safe(QED.qed, rdmol, 0)
        sas = safe(sascore.calculateScore, rdmol, 10)
        sas = (10 - sas) / 9  # Turn into a [0-1] reward
        molwts = safe(Descriptors.MolWt, rdmol, 1000)
        molwts = ((300 - molwts) / 700 + 1)  # 1 until 300 then linear decay to 0 until 1000
        molwts = clamp(molwts, 0, 1)
        flat_rewards = np.array([seh_preds.item(), qeds, sas, molwts])[:self.args.num_objectives]
        if self.args.reward_type == "sum":
            return np.sum(flat_rewards, axis=0) ** self.reward_exp, flat_rewards
        elif self.args.reward_type == "prod":
            return np.prod(flat_rewards, axis=0) ** self.reward_exp, flat_rewards

    def sample(self, n):
        eidx = self.train_rng.randint(0, len(self.train_mols), n)
        samples = [self.train_mols[i] for i in eidx]
        return zip(*samples)

    def sample2batch(self, mb):
        s, a = mb
        s = self.mdp.mols2batch([self.mdp.mol2repr(i) for i in s])
        a = torch.tensor(a, device=self._device).long()
        return s, a

    def log_metrics(self, train_type):
        # Extract flat_rewards from self.sampled_mols where the flat_rewards is at the last index in the list
        # Then pass this array to the MultiObjectiveStatsHook class to calculate the metrics.
        # This returns a dictionary of metrics.

        flat_rewards = np.array([i[-1] for i in self.current_mols])
        rewards, mols, _ = zip(*self.current_mols)
        rdmols = [i.mol for i in mols]
        metrics = self.stats_hook(flat_rewards, rewards, rdmols, train_type)
        # Use wandb to log the metrics
        if self.args.use_wandb:
            wandb.log(metrics)
        else:
            print(metrics)
        self.current_mols = []


_stop = [None]

def main(args):
    bpath = "data/blocks_PDB_105.json"
    device = torch.device('cuda')
    if args.floatX == 'float32':
        args.floatX = torch.float
    else:
        args.floatX = torch.double
    # tf = lambda x: torch.tensor(x, device=device).float()
    tf = lambda x: torch.tensor(x, device=device).to(args.floatX)
    tint = lambda x: torch.tensor(x, device=device).long()

    dataset = Dataset(args, bpath, device, args.repr_type, floatX=args.floatX)

    exp_dir = f'{args.save_path}'
    os.makedirs(exp_dir, exist_ok=True)
    print(args)
    debug_no_threads = True

    mdp = dataset.mdp

    stop_event = threading.Event()
    model = make_model(args, mdp)
    model.to(torch.double)
    model.to(device)

    # Just replace this my reward_proxy.py
    # proxy = Proxy(args, bpath, device)

    dataset.set_sampling_model(model, sample_prob=args.sample_prob)

    opt = torch.optim.Adam(model.parameters(), args.learning_rate,  # weight_decay=1e-4,
                           betas=(args.opt_beta, args.opt_beta2))

    mbsize = args.mbsize
    ar = torch.arange(mbsize)

    num_threads = 8 if not debug_no_threads else 1
    last_losses = []

    def stop_everything():
        stop_event.set()
        print('joining')
    _stop[0] = stop_everything

    def save_stuff():
        pickle.dump([i.data.cpu().numpy() for i in model.parameters()],
                    gzip.open(f'{exp_dir}/params.pkl.gz', 'wb'))

        pickle.dump(dataset.sampled_mols,
                    gzip.open(f'{exp_dir}/sampled_mols.pkl.gz', 'wb'))

        pickle.dump({'train_losses': train_losses,
                     'test_losses': test_losses,
                     'test_infos': test_infos,
                     'time_start': time_start,
                     'time_now': time.time(),
                     'args': args, },
                    gzip.open(f'{exp_dir}/info.pkl.gz', 'wb'))

    train_losses = []
    test_losses = []
    test_infos = []
    time_start = time.time()
    time_last_check = time.time()

    max_early_stop_tolerance = 5
    early_stop_tol = max_early_stop_tolerance
    loginf = 1000  # to prevent nans
    log_reg_c = args.log_reg_c
    clip_loss = tf([args.clip_loss])

    for i in range(args.num_iterations + 1):
        dataset.step_all(num_threads)
        for _ in tqdm(range(args.num_sgd_steps), leave=False):
            s, a = dataset.sample2batch(dataset.sample(mbsize))
            if args.repr_type == 'block_graph':
                stem_out, bond_out = model(s, do_stems=True)
            else:
                stem_out, mol_out, bond_out = model(s, do_bonds=True)
            bs = torch.tensor(s._slice_dict['bonds'])
            ss = torch.tensor(s._slice_dict['stems'])
            loss = 0
            for j in range(mbsize):
                sj = stem_out[ss[j]:ss[j + 1]]
                bj = bond_out[bs[j]:bs[j + 1]]
                cat = SplitCategorical(np.prod(sj.shape),
                                       logits=torch.cat([sj.flatten(), bj.flatten()]))
                lp = cat.log_prob(a[j])
                loss = (loss - lp) / mbsize

            opt.zero_grad()
            loss.backward()
            last_losses.append((loss.item(),))
            train_losses.append((loss.item(),))
            if args.clip_grad > 0:
                torch.nn.utils.clip_grad_value_(model.parameters(),
                                                args.clip_grad)
            opt.step()
        dataset.log_metrics("train")
        model.training_steps = i + 1
        if not i % 10:
            last_losses = [np.round(np.mean(i), 3) for i in zip(*last_losses)]
            print(i, last_losses)
            print('time:', time.time() - time_last_check)
            time_last_check = time.time()
            last_losses = []
            save_stuff()
        if len(dataset.sampled_mols) >= args.max_generated_mols:
            break

    # Sample 5000 molecules for evaluation.
    model.eval()
    dataset = Dataset(args, bpath, device, args.repr_type, floatX=args.floatX)
    dataset.set_sampling_model(model, sample_prob=args.sample_prob)
    dataset.step_all(num_threads)
    dataset.log_metrics("test")
    stop_everything()
    save_stuff()
    print('Done.')


def array_may_17(args):
    base = {'replay_mode': 'online',
            'sample_prob': 0.9,
            'mbsize': 8,
            'nemb': 50,
            'max_blocks': 8,
            }

    all_hps = [
        {**base, 'mbsize': 2, 'buffer_size': 32},
        {**base, 'mbsize': 32, 'buffer_size': 5000, 'num_sgd_steps': 25},
    ]
    return all_hps

def set_seed(seed):
    """Set seed"""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    os.environ["PYTHONHASHSEED"] = str(seed)


if __name__ == '__main__':
    args = parser.parse_args()
    set_seed(args.seed)
    if args.array:
        all_hps = eval(args.array)(args)

        if args.print_array_length:
            print(len(all_hps))
        else:
            hps = all_hps[args.run]
            print(hps)
            for k, v in hps.items():
                setattr(args, k, v)
            main(args)
    else:
        try:
            main(args)
        except KeyboardInterrupt as e:
            print("stopping for", e)
            _stop[0]()
            raise e
        except Exception as e:
            print("exception", e)
            _stop[0]()
            raise e
