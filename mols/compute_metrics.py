


from copy import deepcopy
import math

from botorch.utils.multi_objective import infer_reference_point
from botorch.utils.multi_objective import pareto
from botorch.utils.multi_objective.hypervolume import Hypervolume
from cvxopt import matrix
from cvxopt import solvers
import numpy as np
from rdkit import Chem
from rdkit import DataStructs
import torch


def pareto_frontier(obj_vals, maximize=True):
    """
    Compute the Pareto frontier of a set of candidate solutions.
    ----------
    Parameters
        candidate_pool: NumPy array of candidate objects
        obj_vals: NumPy array of objective values
    ----------
    """
    # pareto utility assumes maximization
    if maximize:
        pareto_mask = pareto.is_non_dominated(torch.tensor(obj_vals))
    else:
        pareto_mask = pareto.is_non_dominated(-torch.tensor(obj_vals))
    return obj_vals[pareto_mask]


def get_hypervolume(flat_rewards: torch.Tensor, zero_ref=True) -> float:
    """Compute the hypervolume of a set of trajectories.
        Parameters
        ----------
        flat_rewards: torch.Tensor
          A tensor of shape (num_trajs, num_of_objectives) containing the rewards of each trajectory.
        """
    # Compute the reference point
    if zero_ref:
        reference_point = torch.zeros_like(flat_rewards[0])
    else:
        reference_point = infer_reference_point(flat_rewards)
    # Compute the hypervolume
    hv_indicator = Hypervolume(reference_point)  # Difference
    return hv_indicator.compute(flat_rewards)


def uniform_reference_points(nobj, p=4, scaling=None):
    """Generate reference points uniformly on the hyperplane intersecting
    each axis at 1. The scaling factor is used to combine multiple layers of
    reference points.
    """
    def gen_refs_recursive(ref, nobj, left, total, depth):
        points = []
        if depth == nobj - 1:
            ref[depth] = left / total
            points.append(ref)
        else:
            for i in range(left + 1):
                ref[depth] = i / total
                points.extend(gen_refs_recursive(ref.copy(), nobj, left - i, total, depth + 1))
        return points

    ref_points = np.array(gen_refs_recursive(np.zeros(nobj), nobj, p, p, 0))
    if scaling is not None:
        ref_points *= scaling
        ref_points += (1 - scaling) / nobj

    return ref_points


def r2_indicator_set(reference_points, solutions, utopian_point):
    """Computer R2 indicator value of a set of solutions (*solutions*) given a set of
    reference points (*reference_points) and a utopian_point (*utopian_point).
        :param reference_points: An array of reference points from a uniform distribution.
        :param solutions: the multi-objective solutions (fitness values).
        :param utopian_point: utopian point that represents best possible solution
        :returns: r2 value (float).
        """

    min_list = []
    for v in reference_points:
        max_list = []
        for a in solutions:
            max_list.append(np.max(v * np.abs(utopian_point - a)))

        min_list.append(np.min(max_list))

    v_norm = np.linalg.norm(reference_points)
    r2 = np.sum(min_list) / v_norm

    return r2


solvers.options['abstol'] = 1e-15
solvers.options['reltol'] = 1e-15
solvers.options['feastol'] = 1e-15
solvers.options['maxiters'] = 1000
solvers.options['show_progress'] = False


def sharpeRatio(p, Q, x, rf):
    """ Compute the Sharpe ratio.
    Returns the Sharpe ratio given the expected return vector, p,
    the covariance matrix, Q, the investment column vector, x, and
    the return of the riskless asset, rf.
    Parameters
    ----------
    p : ndarray
        Expected return vector (of size n).
    Q : ndarray
        Covariance (n,n)-matrix.
    x : ndarray
        Investment vector of size (n,1). The sum of which should be 1.
    rf : float
        Return of a riskless asset.
    Returns
    -------
    sr : float
        The HSR value.
    """
    return (x.T.dot(p) - rf) / math.sqrt(x.T.dot(Q).dot(x))


def _sharpeRatioQPMax(p, Q, rf):
    """ Sharpe ratio maximization problem - QP formulation """
    n = len(p)

    # inequality constraints (investment in assets is higher or equal to 0)
    C = np.diag(np.ones(n))
    d = np.zeros((n, 1), dtype=np.double)

    # equality constraints (just one)
    A = np.zeros((1, n), dtype=np.double)
    b = np.zeros((1, 1), dtype=np.double)
    A[0, :] = p - rf
    b[0, 0] = 1

    # convert numpy matrix to cvxopt matrix
    G, c, A, b, C, d = matrix(Q, tc='d'), matrix(np.zeros(n), tc='d'), matrix(A, tc='d'), matrix(b, tc='d'), matrix(
        C, tc='d'), matrix(d, tc='d')

    sol = solvers.coneqp(G, c, -C, -d, None, A, b, kktsolver='ldl')  # , initvals=self.initGuess)
    y = np.array(sol['x'])

    return y


def sharpeRatioMax(p, Q, rf):
    """ Compute the Sharpe ratio and investment of an optimal portfolio.
    Parameters
    ----------
    p : ndarray
        Expected return vector (of size n).
    Q : ndarray
        Covariance (n,n)-matrix.
    rf : float
        Return of a riskless asset.
    Returns
    -------
    sr : float
        The HSR value.
    x : ndarray
        Investment vector of size (n,1).
    """
    y = _sharpeRatioQPMax(p, Q, rf)
    x = y / y.sum()
    x = np.where(x > 1e-9, x, 0)
    sr = sharpeRatio(p, Q, x, rf)
    return sr, x


# Assumes that l <= A << u
# Assumes A, l, u are numpy arrays
def _expectedReturn(A, low, up):
    """
    Returns the expected return (computed as defined by the HSR indicator), as a
    column vector.
    """
    A = np.array(A, dtype=np.double)  # because of division operator in python 2.7
    return ((up - A).prod(axis=-1)) / ((up - low).prod())


def _covariance(A, low, up, p=None):
    """  Returns the covariance matrix (computed as defined by the HSR indicator). """
    p = _expectedReturn(A, low, up) if p is None else p
    Pmax = np.maximum(A[:, np.newaxis, :], A[np.newaxis, ...])
    P = _expectedReturn(Pmax, low, up)

    Q = P - p[:, np.newaxis] * p[np.newaxis, :]
    return Q


def _argunique(pts):
    """ Find the unique points of a matrix. Returns their indexes. """
    ix = np.lexsort(pts.T)
    diff = (pts[ix][1:] != pts[ix][:-1]).any(axis=1)
    un = np.ones(len(pts), dtype=bool)
    un[ix[1:]] = diff
    return un


def HSRindicator(A, low, up, managedup=False):
    """
    Compute the HSR indicator of the point set A given reference points l and u.
    Returns the HSR value of A given l and u, and returns the optimal investment.
    By default, points in A are assumed to be unique.
    Tip: Either ensure that A does not contain duplicated points
        (for example, remove them previously and then split the
        investment between the copies as you wish), or set the flag
        'managedup' to True.
    Parameters
    ----------
    A : ndarray
        Input matrix (n,d) with n points and d dimensions.
    low : array_like
        Lower reference point.
    up : array_like
        Upper reference point.
    managedup : bool, optional
        If A contains duplicated points and 'managedup' is set to True, only the
        first copy may be assigned positive investment, all other copies are
        assigned zero investment. Otherwise, no special treatment is given to
        duplicate points.
    Returns
    -------
    hsri : float
        The HSR value.
       x : ndarray
        The optimal investment as a column vector array (n,1).
    """
    n = len(A)
    x = np.zeros((n, 1), dtype=float)

    # if u is not strongly dominated by l or A is the empty set
    if (up <= low).any():
        raise ValueError("The lower reference point does not strongly dominate the upper reference point!")

    if len(A) == 0:
        return 0, x

    valid = (A < up).all(axis=1)
    validix = np.where(valid)[0]

    # if A is the empty set
    if valid.sum() == 0:
        return 0, x
    A = A[valid]  # A only contains points that strongly dominate u
    A = np.maximum(A, low)
    m = len(A)  # new size (m <= n)

    # manage duplicate points
    ix = _argunique(A) if managedup else np.ones(m).astype(bool)
    p = _expectedReturn(A[ix], low, up)
    Q = _covariance(A[ix], low, up, p)

    hsri, x[validix[ix]] = sharpeRatioMax(p, Q, 0)

    return hsri, x


class HSR_Calculator:
    def __init__(self, lower_bound, upper_bound, max_obj_bool=None):
        '''
        Class to calculate HSR Indicator with assumption that assumes a maximization on all objectives.
         Parameters
        ----------
        lower_bound : array_like
            Lower reference point.
        upper_bound : array_like
            Upper reference point.
        max_obj_bool : bool, optional
            Details of the objectives for which dimension maximization is not the case.
        '''

        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.max_obj_bool = None

        if max_obj_bool is not None:
            self.max_obj_bool = max_obj_bool

    def reset_lower_bound(self, lower_bound):
        self.lower_bound = lower_bound

    def reset_upper_bound(self, upper_bound):
        self.upper_bound = upper_bound

    def make_max_problem(self, matrix):

        if self.max_obj_bool is None:
            return matrix

        max_matrix = deepcopy(matrix)

        for dim in self.max_obj_bool:
            max_matrix[:, dim] = max_matrix**-1

        return max_matrix

    def calculate_hsr(self, solutions):

        max_solutions = self.make_max_problem(solutions)

        hsr_indicator, hsr_invest = HSRindicator(A=max_solutions, low=self.lower_bound, up=self.upper_bound)

        return hsr_indicator, hsr_invest


class Normalizer(object):
    def __init__(self, loc=0., scale=1.):
        self.loc = loc
        self.scale = np.where(scale != 0, scale, 1.)

    def __call__(self, arr):
        min_val = self.loc - 4 * self.scale
        max_val = self.loc + 4 * self.scale
        clipped_arr = np.clip(arr, a_min=min_val, a_max=max_val)
        norm_arr = (clipped_arr - self.loc) / self.scale

        return norm_arr

    def inv_transform(self, arr):
        return self.scale * arr + self.loc


# Should be calculated per preference
def compute_diverse_top_k(rdmols, rewards, k, thresh=0.7):
    # mols is a list of (reward, mol)
    mols = []
    for i in range(len(rdmols)):
        mols.append([rewards[i].item(), rdmols[i]])
    mols = sorted(mols, key=lambda m: m[0], reverse=True)
    modes = [mols[0]]
    mode_fps = [Chem.RDKFingerprint(mols[0][1])]
    for i in range(1, len(mols)):
        fp = Chem.RDKFingerprint(mols[i][1])
        sim = DataStructs.BulkTanimotoSimilarity(fp, mode_fps)
        if max(sim) < thresh:
            modes.append(mols[i])
            mode_fps.append(fp)
        if len(modes) >= k:
            # last_idx = i
            break
    return np.mean([i[0] for i in modes])  # return sim


def get_topk(rewards, k):
    '''
     Parameters
    ----------
    rewards : array_like
        Rewards obtained after taking the convex combination.
        Shape: number_of_preferences x number_of_samples
    k : int
        Tok k value

    Returns
    ----------
    avergae Topk rewards across all preferences
    '''
    topk_rewards, topk_indices = torch.topk(torch.FloatTensor(rewards), k)
    mean_topk_rewards = torch.mean(topk_rewards, dim=0).item()
    return mean_topk_rewards


class MultiObjectiveStatsHook:
    def __init__(self, num_to_keep: int, num_objs: int):
        self.num_to_keep = num_to_keep
        self.all_flat_rewards: List[Tensor] = []
        self.hsri_epsilon = 0.3
        self.uniform_reference_points = uniform_reference_points(num_objs, p=12)
        self.utopian_point = np.ones(num_objs)

    def __call__(self, flat_rewards, rewards, sampled_molecules, train_type):
        flat_rewards = torch.tensor(flat_rewards)
        self.all_flat_rewards = self.all_flat_rewards + list(flat_rewards)
        if len(self.all_flat_rewards) > self.num_to_keep:
            self.all_flat_rewards = self.all_flat_rewards[-self.num_to_keep:]

        flat_rewards = torch.stack(self.all_flat_rewards).numpy()
        target_min = flat_rewards.min(0).copy()
        target_range = flat_rewards.max(0).copy() - target_min
        hypercube_transform = Normalizer(
            loc=target_min,
            scale=target_range,
        )
        gfn_pareto = pareto_frontier(flat_rewards)
        normed_gfn_pareto = hypercube_transform(gfn_pareto)
        hypervolume_with_zero_ref = get_hypervolume(torch.tensor(normed_gfn_pareto), zero_ref=True)
        hypervolume_wo_zero_ref = get_hypervolume(torch.tensor(normed_gfn_pareto), zero_ref=False)
        # unnorm_hypervolume_with_zero_ref = get_hypervolume(torch.tensor(gfn_pareto), zero_ref=True)
        # unnorm_hypervolume_wo_zero_ref = get_hypervolume(torch.tensor(gfn_pareto), zero_ref=False)

        # upper = np.zeros(normed_gfn_pareto.shape[-1]) + self.hsri_epsilon
        # lower = np.ones(normed_gfn_pareto.shape[-1]) * -1 - self.hsri_epsilon
        # hsr_indicator = HSR_Calculator(lower, upper)
        # try:
        #     hsri_w_pareto, x = hsr_indicator.calculate_hsr(-1 * gfn_pareto)
        # except Exception:
        #     hsri_w_pareto = 0
        # try:
        #     hsri_on_flat, _ = hsr_indicator.calculate_hsr(-1 * flat_rewards)
        # except Exception:
        #     hsri_on_flat = 0
        # r2_score = r2_indicator_set(self.uniform_reference_points, flat_rewards, self.utopian_point)
        # topk_diversity = compute_diverse_top_k(sampled_molecules[-self.num_to_keep:], rewards[-self.num_to_keep:], 100)
        # topk_rewards = get_topk(rewards, 100)
        return {
            'HV with zero ref ' + train_type: hypervolume_with_zero_ref,
            'HV w/o zero ref ' + train_type: hypervolume_wo_zero_ref,
            # 'Unnormalized HV with zero ref': unnorm_hypervolume_with_zero_ref,
            # 'Unnormalized HV w/o zero ref': unnorm_hypervolume_wo_zero_ref,
            # 'hsri_with_pareto': hsri_w_pareto,
            # 'hsri_on_flat_rew': hsri_on_flat,
            # 'r2_score': r2_score,
            # 'topk_diversity': topk_diversity,
            # 'topk_rewards': topk_rewards,
        }


if __name__ == "__main__":

    # Example for 2 dimensions
    # Point set: {(1,3), (2,2), (3,1)},  l = (0,0), u = (4,4)
    A = np.array([[1, 3], [2, 2], [3, 1]])  # matrix with dimensions n x d (n points, d dimensions)
    low = np.zeros(2)  # l must weakly dominate every point in A
    up = np.array([4, 4])  # u must be strongly dominated by every point in A

    # A = np.array([[3.41e-01, 9.72e-01, 2.47e-01],
    #              [9.30e-01, 1.53e-01, 4.72e-01],
    #              [4.56e-01, 1.71e-01, 8.68e-01],
    #              [8.70e-02, 5.94e-01, 9.50e-01],
    #              [5.31e-01, 6.35e-01, 1.95e-01],
    #              [3.12e-01, 3.37e-01, 7.01e-01],
    #              [3.05e-02, 9.10e-01, 7.71e-01],
    #              [8.89e-01, 8.29e-01, 2.07e-02],
    #              [6.92e-01, 3.62e-01, 2.93e-01],
    #              [2.33e-01, 4.55e-01, 6.60e-01]])
    #
    # l = np.zeros(3)  # l must weakly dominate every point in A
    # u = np.array([1, 1, 1])

    hsr_class = HSR_Calculator(lower_bound=low, upper_bound=up)
    hsri, x = hsr_class.calculate_hsr(A)  # compute HSR indicator

    print("Optimal investment:")
    print("%s" % "\n".join(map(str, x[:, 0])))
    print("HSR indicator value: %f" % hsri)