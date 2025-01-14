import torch
from torch.optim.optimizer import Optimizer
from OPTAMI.utils import tuple_to_vec, derivatives, line_search


class CubicRegularizedNewton(Optimizer):
    """Implements Cubic Regularized Newton Method.
    It had been proposed in `Cubic regularization of Newton method and its global performance`
    https://link.springer.com/content/pdf/10.1007/s10107-006-0706-8.pdf
    Contributors:
        Dmitry Kamzolov
        Dmitry Vilensky-Pasechnyuk
    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining parameter groups
        L (float): estimated value of Lipschitz constant of the Hessian
        subsolver (Optimizer): optimization method to solve the inner problem by gradient steps
        subsolver_args (dict): arguments for the subsolver such as a learning rate and others
        max_iters (int): number of the inner iterations of the subsolver to solve the inner problem
        rel_acc (float): relative stopping criterion for the inner problem
    """
    MONOTONE = True

    def __init__(self, params, L: float = 1., subsolver: Optimizer = None,
                 subsolver_args: dict = None, max_iters: int = 100,
                 rel_acc: float = 1e-1, verbose: bool = True, testing: bool = False,
                 lazy_args: dict = dict(step_per_update=1)):
        if L <= 0:
            raise ValueError(f"Invalid learning rate: L = {L}")
        
        lazy_args['cur_iter'] = 0
        lazy_args['cached_hess'] = 0

        super().__init__(params, dict(
            L=L, subsolver=subsolver,
            subsolver_args=subsolver_args or {'lr': 1e-2},
            max_iters=max_iters, rel_acc=rel_acc, lazy_args=lazy_args))

        self.verbose = verbose
        self.testing = testing
        self.hess = None

    def step(self, closure, hess=True):
        """Performs a single optimization step.
        Arguments:
            closure (callable): a closure that reevaluates the model and returns the loss
        """
        closure = torch.enable_grad()(closure)

        for group in self.param_groups:
            params = group['params']
            L = group['L']
            rel_acc = group['rel_acc']
            max_iters = group['max_iters']
            subsolver = group['subsolver']
            subsolver_args = group['subsolver_args']
            
            if not hess:
                precalc = self.cached_hess
            else:
                precalc = None
            
            if subsolver is None:
                x, self.cached_hess = exact(params, closure, L, precalc, testing=self.testing)
            else:
                is_satisfactory, x = iterative(
                    params, closure, L,
                    subsolver, subsolver_args, max_iters, rel_acc)

                if not is_satisfactory and self.verbose:
                    print('subproblem was solved inaccurately')

            with torch.no_grad():
                for i, p in enumerate(params):
                    p.add_(x[i])
            
            group['lazy_args']['cur_iter'] += 1
            
        return None


def exact(params, closure, L, hess=None, delta=1e-8, testing=False):
    df = tuple_to_vec.tuple_to_vector(
        torch.autograd.grad(closure(), list(params), create_graph=True))
    
    if hess is not None:
        H = hess
    else:
        H = derivatives.flat_hessian(df, list(params)).to(torch.double)

    c = df.detach().to(torch.double)
    A = H.detach().to(torch.double)

    if c.dim() != 1:
        raise ValueError(f"`c` must be a vector, but c = {c}")

    if A.dim() > 2:
        raise ValueError(f"`A` must be a matrix, but A = {A}")

    if c.size()[0] != A.size()[0]:
        raise ValueError("`c` and `A` mush have the same 1st dimension")

    if (A.t() - A).max() > 0.1:
        raise ValueError("`A` is not symmetric")

    T, U = torch.linalg.eigh(A)
    ct = U.t().mv(c)

    def inv(T, L, tau):
        return (T + L / 2 * tau).reciprocal()

    def dual(tau):
        return L / 12 * tau.pow(3) + 1 / 2 * \
               inv(T, L, tau).mul(ct.square()).sum()

    tau_best = line_search.ray_line_search(
        dual,
        left_point=torch.tensor([0.], device=T.device),
        middle_point=torch.tensor([2.], device=T.device),
        delta=delta)

    invert = inv(T, L, tau_best)
    x = -U.mv(invert.mul(ct).type_as(U))

    if testing and (c + L / 2 * x.norm() * x + A.mv(x)).abs().max().item() >= 0.01:
        raise ValueError('obtained `x` is not optimal')

    return tuple_to_vec.rollup_vector(x, list(params)), H


def iterative(params, closure, L, subsolver, subsolver_args, max_iters, rel_acc):
    x = torch.zeros_like(tuple_to_vec.tuple_to_vector(
        list(params)), requires_grad=True)
    optimizer = subsolver([x], **subsolver_args)

    for _ in range(max_iters):
        optimizer.zero_grad()
        Hx, df = derivatives.flat_hvp(closure, list(params), x)

        for p in params:
            if p.grad is not None:
                if p.grad.grad_fn is not None:
                    p.grad.detach_()
                else:
                    p.grad.requires_grad_(False)
                p.grad.zero_()

        x.grad = df + Hx + x.mul(L * x.norm() / 2.)
        optimizer.step()

        if x.grad.norm() < rel_acc * df.norm():
            return True, tuple_to_vec.rollup_vector(x.detach(), list(params))

    return False, tuple_to_vec.rollup_vector(x.detach(), list(params))
