# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Quadratic programming with equality constraints only."""

from typing import Any
from typing import Callable
from typing import Optional
from typing import Tuple

from dataclasses import dataclass

import jax
import jax.numpy as jnp 

from jaxopt._src import base
from jaxopt._src import implicit_diff as idf
from jaxopt._src import linear_solve
from jaxopt._src import tree_util
from jaxopt._src.linear_operator import _make_linear_operator
from jaxopt._src.cvxpy_wrapper import _check_params


def _make_eq_qp_optimality_fun(matvec_Q, matvec_A):
  """Makes the optimality function for quadratic programming.

  Returns:
    optimality_fun(params, params_obj, params_eq, params_ineq) where
      params = (primal_var, eq_dual_var, None)
      params_obj = (params_Q, c)
      params_eq = (params_A, b)
  """
  def obj_fun(primal_var, params_obj):
    params_Q, c = params_obj
    Q = matvec_Q(params_Q)
    return (0.5 * tree_util.tree_vdot(primal_var, Q(primal_var)) +
            tree_util.tree_vdot(primal_var, c))

  def eq_fun(primal_var, params_eq):
    params_A, b = params_eq
    A = matvec_A(params_A)
    return tree_util.tree_sub(A(primal_var), b)

  optimality_fun_with_ineq =  idf.make_kkt_optimality_fun(obj_fun, eq_fun, ineq_fun=None)

  # It is required to post_process the output of `idf.make_kkt_optimality_fun`
  # to make the signatures of optimality_fun() and run() agree.
  def optimality_fun(params, params_obj, params_eq):
    return optimality_fun_with_ineq(params, params_obj, params_eq, None)
  
  return optimality_fun


@dataclass(eq=False)
class EqualityConstrainedQP(base.Solver):
  """Quadratic programming with equality constraints only.

  Supports implicit differentiation, matvec and pytrees.
  Can benefit from GPU/TPU acceleration.

  Not as precise as CVXPY_QP.

  Attributes:
    matvec_Q: a Callable matvec_Q(params_Q, u).
      By default, matvec_Q(Q, u) = dot(Q, u), where Q = params_Q.
    matvec_A: a Callable matvec_A(params_A, u).
      By default, matvec_A(A, u) = dot(A, u), where A = params_A.
    solve: a Callable to solve linear systems, that accepts matvecs (default: linear_solve.solve_gmres).
    maxiter: maximum number of iterations.
    tol: tolerance (stoping criterion).
    implicit_diff: whether to enable implicit diff or autodiff of unrolled
      iterations.
    implicit_diff_solve: the linear system solver to use.
    jit: whether to JIT-compile the optimization loop (default: "auto").
  """
  matvec_Q: Optional[Callable] = None
  matvec_A: Optional[Callable] = None
  solve: Callable = linear_solve.solve_gmres
  maxiter: int = 1000
  tol: float = 1e-5
  implicit_diff_solve: Optional[Callable] = None
  jit: bool = True

  def run(self,
          init_params: Optional[Any] = None,
          params_obj: Optional[Any] = None,
          params_eq: Optional[Any] = None) -> base.OptStep:
    """Solves 0.5 * x^T Q x + c^T x subject to Ax = b.

    This solver returns both the primal solution (x) and the dual solution.

    Args:
      init_params: ignored.
      params_obj: (Q, c) or (params_Q, c) if matvec_Q is provided.
      params_eq: (A, b) or (params_A, b) if matvec_A is provided.
    Returns:
      (params, state),  where params = (primal_var, dual_var_eq, None)
    """
    del init_params  # no warm start
    if self._check_params:
      _check_params(params_obj, params_eq)

    params_Q, c = params_obj
    params_A, b = params_eq

    Q = self.matvec_Q(params_Q)
    A = self.matvec_A(params_A)

    def matvec(u):
        primal_u, dual_u = u
        mv_A, rmv_A = A.matvec_and_rmatvec(primal_u, dual_u)
        return (tree_util.tree_add(Q(primal_u), rmv_A), mv_A)

    minus_c = tree_util.tree_negative(c)

    # Solves the following linear system:
    # [[Q A^T]  [primal_var = [-c
    #  [A 0  ]]  dual_var  ]    b]
    primal, dual_eq = self.solve(matvec, (minus_c, b), tol=self.tol, maxiter=self.maxiter)
    return base.OptStep(params=base.KKTSolution(primal, dual_eq, None), state=None)

  def l2_optimality_error(self,
    params: jnp.array,
    params_obj: Any,
    params_eq: Any):
    """Computes the L2 norm of the KKT residuals."""
    tree = self.optimality_fun(params, params_obj, params_eq)
    return tree_util.tree_l2_norm(tree)

  def __post_init__(self):
    self._check_params = self.matvec_Q is None and self.matvec_A is None

    self.matvec_Q = _make_linear_operator(self.matvec_Q)
    self.matvec_A = _make_linear_operator(self.matvec_A)

    self.optimality_fun = _make_eq_qp_optimality_fun(self.matvec_Q, self.matvec_A)

    # Set up implicit diff.
    decorator = idf.custom_root(self.optimality_fun, has_aux=True,
                                solve=self.implicit_diff_solve)
    # pylint: disable=g-missing-from-attributes
    self.run = decorator(self.run)

    if self.jit:
      self.run = jax.jit(self.run)