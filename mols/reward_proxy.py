import gzip
import os
import pickle  # nosec

import numpy as np
from rdkit import RDConfig
from rdkit.Chem import ChemicalFeatures
from rdkit.Chem.rdchem import BondType as BT
from rdkit.Chem.rdchem import HybridizationType
import requests  # type: ignore
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Batch
from torch_geometric.data import Data
from torch_geometric.nn import NNConv
from torch_geometric.nn import Set2Set
from torch_sparse import coalesce

NUM_ATOMIC_NUMBERS = 56  # Number of atoms used in the molecules (i.e. up to Ba)


class MPNNet(nn.Module):
    def __init__(self, num_feat=14, num_vec=3, dim=64, num_out_per_mol=1, num_out_per_stem=105, num_out_per_bond=1,
                 num_conv_steps=12):
        super().__init__()
        self.lin0 = nn.Linear(num_feat + num_vec, dim)
        self.num_ops = num_out_per_stem
        self.num_opm = num_out_per_mol
        self.num_conv_steps = num_conv_steps
        self.dropout_rate = 0

        self.act = nn.LeakyReLU()

        net = nn.Sequential(nn.Linear(4, 128), self.act, nn.Linear(128, dim * dim))
        self.conv = NNConv(dim, dim, net, aggr='mean')
        self.gru = nn.GRU(dim, dim)

        self.set2set = Set2Set(dim, processing_steps=3)
        self.lin3 = nn.Linear(dim * 2, num_out_per_mol)
        self.bond2out = nn.Sequential(nn.Linear(dim * 2, dim), self.act, nn.Linear(dim, dim), self.act,
                                      nn.Linear(dim, num_out_per_bond))

    def forward(self, data, do_dropout=False):
        out = self.act(self.lin0(data.x))
        h = out.unsqueeze(0)
        h = F.dropout(h, training=do_dropout, p=self.dropout_rate)

        for i in range(self.num_conv_steps):
            m = self.act(self.conv(out, data.edge_index, data.edge_attr))
            m = F.dropout(m, training=do_dropout, p=self.dropout_rate)
            out, h = self.gru(m.unsqueeze(0).contiguous(), h.contiguous())
            h = F.dropout(h, training=do_dropout, p=self.dropout_rate)
            out = out.squeeze(0)

        global_out = self.set2set(out, data.batch)
        global_out = F.dropout(global_out, training=do_dropout, p=self.dropout_rate)
        per_mol_out = self.lin3(global_out)  # per mol scalar outputs
        return per_mol_out


def load_original_model(pretrained_path):
    num_feat = (14 + 1 + NUM_ATOMIC_NUMBERS)
    mpnn = MPNNet(num_feat=num_feat, num_vec=0, dim=64, num_out_per_mol=1, num_out_per_stem=105, num_conv_steps=12)
#    f = requests.get("https://github.com/GFNOrg/gflownet/raw/master/mols/data/pretrained_proxy/best_params.pkl.gz",
#                     stream=True)
    with gzip.open(pretrained_path + "/best_params.pkl.gz", "rb") as f:
        params = pickle.load(f)
    param_map = {
        'lin0.weight': params[0],
        'lin0.bias': params[1],
        'conv.bias': params[3],
        'conv.nn.0.weight': params[4],
        'conv.nn.0.bias': params[5],
        'conv.nn.2.weight': params[6],
        'conv.nn.2.bias': params[7],
        'conv.lin.weight': params[2],
        'gru.weight_ih_l0': params[8],
        'gru.weight_hh_l0': params[9],
        'gru.bias_ih_l0': params[10],
        'gru.bias_hh_l0': params[11],
        'set2set.lstm.weight_ih_l0': params[16],
        'set2set.lstm.weight_hh_l0': params[17],
        'set2set.lstm.bias_ih_l0': params[18],
        'set2set.lstm.bias_hh_l0': params[19],
        'lin3.weight': params[20],
        'lin3.bias': params[21],
    }
    for k, v in param_map.items():
        mpnn.get_parameter(k).data = torch.tensor(v)
    return mpnn


_mpnn_feat_cache = [None]


def mpnn_feat(mol, ifcoord=True, panda_fmt=False, one_hot_atom=False, donor_features=False):
    atomtypes = {'H': 0, 'C': 1, 'N': 2, 'O': 3, 'F': 4}
    bondtypes = {BT.SINGLE: 0, BT.DOUBLE: 1, BT.TRIPLE: 2, BT.AROMATIC: 3, BT.UNSPECIFIED: 0}

    natm = len(mol.GetAtoms())
    ntypes = len(atomtypes)
    # featurize elements
    # columns are: ["type_idx" .. , "atomic_number", "acceptor", "donor",
    # "aromatic", "sp", "sp2", "sp3", "num_hs", [atomic_number_onehot] .. ])

    nfeat = ntypes + 1 + 8
    if one_hot_atom:
        nfeat += NUM_ATOMIC_NUMBERS
    atmfeat = np.zeros((natm, nfeat))

    # featurize
    for i, atom in enumerate(mol.GetAtoms()):
        type_idx = atomtypes.get(atom.GetSymbol(), 5)
        atmfeat[i, type_idx] = 1
        if one_hot_atom:
            atmfeat[i, ntypes + 9 + atom.GetAtomicNum() - 1] = 1
        else:
            atmfeat[i, ntypes + 1] = (atom.GetAtomicNum() % 16) / 2.
        atmfeat[i, ntypes + 4] = atom.GetIsAromatic()
        hybridization = atom.GetHybridization()
        atmfeat[i, ntypes + 5] = hybridization == HybridizationType.SP
        atmfeat[i, ntypes + 6] = hybridization == HybridizationType.SP2
        atmfeat[i, ntypes + 7] = hybridization == HybridizationType.SP3
        atmfeat[i, ntypes + 8] = atom.GetTotalNumHs(includeNeighbors=True)

    # get donors and acceptors
    if donor_features:
        if _mpnn_feat_cache[0] is None:
            fdef_name = os.path.join(RDConfig.RDDataDir, 'BaseFeatures.fdef')
            factory = ChemicalFeatures.BuildFeatureFactory(fdef_name)
            _mpnn_feat_cache[0] = factory
        else:
            factory = _mpnn_feat_cache[0]
        feats = factory.GetFeaturesForMol(mol)
        for j in range(0, len(feats)):
            if feats[j].GetFamily() == 'Donor':
                node_list = feats[j].GetAtomIds()
                for k in node_list:
                    atmfeat[k, ntypes + 3] = 1
            elif feats[j].GetFamily() == 'Acceptor':
                node_list = feats[j].GetAtomIds()
                for k in node_list:
                    atmfeat[k, ntypes + 2] = 1
    # get coord
    if ifcoord:
        coord = np.asarray([mol.GetConformer(0).GetAtomPosition(j) for j in range(natm)])
    else:
        coord = None
    # get bonds and bond features
    bond = np.asarray([[bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()] for bond in mol.GetBonds()])
    bondfeat = [bondtypes[bond.GetBondType()] for bond in mol.GetBonds()]
    bondfeat = onehot(bondfeat, num_classes=len(bondtypes) - 1)

    return atmfeat, coord, bond, bondfeat


def mol_to_graph_backend(atmfeat, coord, bond, bondfeat, props={}, data_cls=Data):
    "convert to PyTorch geometric module"
    natm = atmfeat.shape[0]
    # transform to torch_geometric bond format; send edges both ways; sort bonds
    atmfeat = torch.tensor(atmfeat, dtype=torch.float32)
    if bond.shape[0] > 0:
        edge_index = torch.tensor(np.concatenate([bond.T, np.flipud(bond.T)], axis=1), dtype=torch.int64)
        edge_attr = torch.tensor(np.concatenate([bondfeat, bondfeat], axis=0), dtype=torch.float32)
        edge_index, edge_attr = coalesce(edge_index, edge_attr, natm, natm)
    else:
        edge_index = torch.zeros((0, 2), dtype=torch.int64)
        edge_attr = torch.tensor(bondfeat, dtype=torch.float32)

    # make torch data
    if coord is not None:
        coord = torch.tensor(coord, dtype=torch.float32)
        data = data_cls(x=atmfeat, pos=coord, edge_index=edge_index, edge_attr=edge_attr, **props)
    else:
        data = data_cls(x=atmfeat, edge_index=edge_index, edge_attr=edge_attr, **props)
    return data


def onehot(arr, num_classes, dtype=np.int):
    arr = np.asarray(arr, dtype=np.int)
    assert len(arr.shape) == 1, "dims other than 1 not implemented"
    onehot_arr = np.zeros(arr.shape + (num_classes,), dtype=dtype)
    onehot_arr[np.arange(arr.shape[0]), arr] = 1
    return onehot_arr


def mol2graph(mol, floatX=torch.float, bonds=False, nblocks=False):
    rdmol = mol
    if rdmol is None:
        g = Data(x=torch.zeros((1, 14 + NUM_ATOMIC_NUMBERS)), edge_attr=torch.zeros((0, 4)), edge_index=torch.zeros(
            (0, 2)).long())
    else:
        atmfeat, _, bond, bondfeat = mpnn_feat(mol, ifcoord=False, one_hot_atom=True, donor_features=False)
        g = mol_to_graph_backend(atmfeat, None, bond, bondfeat)
    stem_mask = torch.zeros((g.x.shape[0], 1))
    g.x = torch.cat([g.x, stem_mask], 1).to(floatX)
    g.edge_attr = g.edge_attr.to(floatX)
    if g.edge_index.shape[0] == 0:
        g.edge_index = torch.zeros((2, 1)).long()
        g.edge_attr = torch.zeros((1, g.edge_attr.shape[1])).to(floatX)
    return g


def mols2batch(mols):
    batch = Batch.from_data_list(mols)
    return batch
