import numpy as np
import tensorflow as tf

# Load the library containing the custom operation
mod = tf.load_op_library('rime.so')

def sum_coherencies_op(src_step, *args):
    uvw_dtype = args[0].dtype.base_dtype

    if uvw_dtype == tf.float32:
        CT = tf.complex64
    elif uvw_dtype == tf.float64:
        CT = tf.complex128
    else:
        raise TypeError("Unhandled type '{t}'".format(t=lm.dtype))

    # Model visibilities that we accumulate over
    model_vis = tf.zeros([ntime, nbl, nchan, 4], dtype=CT)

    # For adding model visibilities and
    # lower and upper source extents to the args
    new_args = lambda m, l, u: args + (m,l,u)

    # l from 0 until nsrc
    l = tf.constant(0)
    cond = lambda l, m: tf.less(l, nsrc)

    # At each iteration compute model vis,
    # utilising model vis from previous iteration
    def body(l, m):
        u = tf.minimum(tf.add(l, src_step), nsrc)
        return u, mod.rime_sum_coherencies(*new_args(m,l,u))

    r = tf.while_loop(cond, body, [l, model_vis])

    # Return final model visibilities
    return r[1]

ntime, na, nchan = 20, 7, 32
nbl = na*(na-1)//2
dtype = np.float32
npsrc, ngsrc, nssrc = 20, 20, 20
nsrc = npsrc+ngsrc+nssrc
src_step = 5

rf = lambda *s: np.random.random(size=s).astype(dtype)

np_uvw = rf(ntime, na, 3)
np_gauss_shape = rf(ngsrc, 3)
np_sersic_shape = rf(nssrc, 3)
np_frequency = rf(nchan)
np_ant1, np_ant2 = map(lambda x: np.int32(x), np.triu_indices(na, 1))
np_ant1, np_ant2 = np.tile(np_ant1, ntime), np.tile(np_ant2, ntime)
np_ant_jones = rf(nsrc, ntime, na, nchan, 4) + rf(nsrc, ntime, na, nchan, 4)*1j
np_flag = np.zeros(shape=(ntime, nbl, nchan, 4)).astype(np.uint8)
np_weight = rf(ntime, nbl, nchan, 4)
np_g_term = rf(ntime, na, nchan, 4) + rf(ntime, na, nchan, 4)*1j
np_obs_vis = rf(ntime, nbl, nchan, 4) + rf(ntime, nbl, nchan, 4)*1j

args = map(lambda n, s: tf.Variable(n, name=s),
    [np_uvw, np_gauss_shape, np_sersic_shape,
    np_frequency, np_ant1, np_ant2, np_ant_jones,
    np_flag, np_weight, np_g_term,
    np_obs_vis],
    ["uvw", "gauss_shape", "sersic_shape",
    "frequency", "ant1", "ant2", "ant_jones",
    "flag", "weight", "g_term",
    "observed_vis"])

tf_src_step = tf.Variable(src_step)

with tf.device('/cpu:0'):
    sum_coh_op_cpu = sum_coherencies_op(tf_src_step, *args)

with tf.device('/gpu:0'):
    sum_coh_op_gpu = sum_coherencies_op(tf_src_step, *args)
    args[-1] = sum_coh_op_gpu
    sum_coh_op_gpu = sum_coherencies_op(tf_src_step, *args)

with tf.Session() as S:
    S.run(tf.initialize_all_variables())

    tf_sum_coh_op_cpu = S.run(sum_coh_op_cpu)
    tf_sum_coh_op_gpu = S.run(sum_coh_op_gpu)

    print tf_sum_coh_op_gpu.flatten()[0:20]

    #assert np.allclose(tf_sum_coh_op_gpu, np.array([nsrc*2 + 0*1j]))