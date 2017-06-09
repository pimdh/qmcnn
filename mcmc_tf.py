"""Test module."""
from __future__ import division
import tensorflow as tf
import numpy as np
from indices_numpy import create_index_matrix

tf.reset_default_graph()

K = 25
ALPHA = 4
SCALE = 1E-2
SYSTEM_SHAPE = (20,)
N_DIMS = len(SYSTEM_SHAPE)
NUM_SPINS = np.prod(SYSTEM_SHAPE)
FULL_WINDOW_SHAPE = (K*2-1,)*N_DIMS
FULL_WINDOW_SIZE = np.prod(FULL_WINDOW_SHAPE)
HALF_WINDOW_SHAPE = (K,)*N_DIMS
HALF_WINDOW_SIZE = np.prod(HALF_WINDOW_SHAPE)
BATCH_SIZE = 1000
H = 1.0

# NUM_SAMPLES = 4
# NUM_SAMPLERS = 2
NUM_SAMPLES = 10000
NUM_SAMPLERS = 1000
ITS_PER_SAMPLE = NUM_SPINS
SAMPLES_PER_SAMPLER = NUM_SAMPLES // NUM_SAMPLERS
THERM_ITS = SAMPLES_PER_SAMPLER // 2
TOTAL_ITS = THERM_ITS + (SAMPLES_PER_SAMPLER-1) * ITS_PER_SAMPLE + 1


def unpad(x, pad_size):
    """Unpad tensor. Pad size is the padding in any direction."""
    size = tf.shape(x)[1:]
    if isinstance(pad_size, int):
        pad_size = [pad_size]*N_DIMS
    slice_start = [0] + pad_size
    slice_size = [-1]+[size[d]-pad_size[d]*2 for d in range(N_DIMS)]
    return tf.slice(x, slice_start, slice_size)


def pad(x, pad_size):
    """Add wrapped padding."""
    dtype = x.dtype
    res = unpad(tf.tile(tf.cast(x, tf.int32), [1]+[3]*N_DIMS),
                [s-pad_size for s in SYSTEM_SHAPE])
    return tf.cast(res, dtype)


def all_windows(x, window_shape):
    """Get all windows."""
    index_matrix = tf.constant(create_index_matrix(SYSTEM_SHAPE, window_shape))
    return tf.transpose(
        tf.gather_nd(tf.transpose(x),
                     tf.expand_dims(index_matrix, 2)),
        [2, 0, 1])


def alignedness(states):
    """Sum of product of neighbouring spins."""
    indices = np.arange(NUM_SPINS).reshape(SYSTEM_SHAPE)
    interactions = []
    for i in range(N_DIMS):
        shifted = tf.transpose(tf.gather(
            tf.transpose(states), np.roll(indices, 1, i).flatten()))
        interactions.append(tf.reduce_sum(states * shifted, 1))
    return tf.reduce_sum(tf.stack(interactions, 1), 1)


def create_vars():
    """Add vars to graph."""
    with tf.variable_scope("factors"):
        tf.get_variable(
            "filters", shape=[K]*N_DIMS+[1]+[2*ALPHA],
            initializer=tf.random_normal_initializer(0., SCALE))
        tf.get_variable(
            "bias_vis", shape=[2],
            initializer=tf.random_normal_initializer(0., SCALE))
        tf.get_variable(
            "bias_hid", shape=[2*ALPHA],
            initializer=tf.random_normal_initializer(0., SCALE))

    with tf.variable_scope("sampler"):
        tf.get_variable("current_samples",
                        shape=[NUM_SAMPLERS, NUM_SPINS],
                        initializer=tf.constant_initializer(0),
                        dtype=tf.int8, trainable=False)
        tf.get_variable("current_factors",
                        shape=[NUM_SAMPLERS, NUM_SPINS],
                        initializer=tf.constant_initializer(0),
                        dtype=tf.complex64, trainable=False)
        tf.get_variable("samples",
                        shape=[SAMPLES_PER_SAMPLER, NUM_SAMPLERS, NUM_SPINS],
                        initializer=tf.constant_initializer(0),
                        dtype=tf.int8, trainable=False)
        tf.get_variable("flip_positions", shape=[TOTAL_ITS, NUM_SAMPLERS],
                        initializer=tf.constant_initializer(0),
                        dtype=tf.int32, trainable=False)


def factors_op(x):
    """Compute model factors."""
    with tf.variable_scope("factors", reuse=True):
        filters = tf.get_variable("filters")
        bias_vis = tf.get_variable("bias_vis")
        bias_hid = tf.get_variable("bias_hid")

    x_float = tf.cast(x, tf.float32)
    x_unpad = unpad(x_float, (K-1)//2)
    x_expanded = x_float[..., None]

    if N_DIMS == 1:
        theta = tf.nn.conv1d(x_expanded, filters, 1, 'VALID')+bias_hid
    elif N_DIMS == 2:
        theta = tf.nn.conv2d(x_expanded, filters, [1]*4, 'VALID')+bias_hid

    theta = tf.complex(theta[..., :ALPHA], theta[..., ALPHA:])
    activation = tf.log(tf.exp(theta) + tf.exp(-theta))
    bias = tf.complex(bias_vis[0] * x_unpad, bias_vis[1] * x_unpad)
    return tf.reduce_sum(activation, N_DIMS+1) + bias


def loss_op(factors, energies):
    """Compute loss."""
    n = tf.cast(BATCH_SIZE, tf.complex64)
    energies = tf.cast(energies, tf.complex64)
    log_psi = tf.reduce_sum(factors, range(1, N_DIMS+1))
    log_psi_conj = tf.conj(log_psi)
    energy_avg = tf.reduce_sum(energies)/n
    return tf.reduce_sum(energies*log_psi_conj, 0)/n - \
        energy_avg * tf.reduce_sum(log_psi_conj, 0)/n


def energy_op(states):
    """Compute local energy of states."""
    states_shaped = tf.reshape(states, (BATCH_SIZE,)+SYSTEM_SHAPE)
    factors = tf.reshape(factors_op(pad(states_shaped, K-1)), (BATCH_SIZE, -1))
    factor_windows = all_windows(factors, HALF_WINDOW_SHAPE)
    spin_windows = all_windows(states, FULL_WINDOW_SHAPE)
    flipper = np.ones(FULL_WINDOW_SIZE, dtype=np.int8)
    flipper[(FULL_WINDOW_SIZE-1)//2] = -1
    spins_flipped = spin_windows * flipper
    factors_flipped = tf.reshape(
        factors_op(tf.reshape(
            spins_flipped, (BATCH_SIZE*NUM_SPINS,)+FULL_WINDOW_SHAPE)),
        (BATCH_SIZE, NUM_SPINS, -1))

    log_pop = tf.reduce_sum(factors_flipped - factor_windows, 2)
    energy = -H * tf.reduce_sum(tf.exp(log_pop), 1) - \
        tf.cast(alignedness(states), tf.complex64)
    return energy / NUM_SPINS


def mcmc_reset():
    """Reset MCMC variables."""
    with tf.variable_scope('sampler', reuse=True):
        current_samples = tf.get_variable('current_samples', dtype=tf.int8)
        current_factors = tf.get_variable('current_factors',
                                          dtype=tf.complex64)
        samples = tf.get_variable('samples', dtype=tf.int8)
        flip_positions = tf.get_variable('flip_positions', dtype=tf.int32)

    states = tf.random_uniform(
        [NUM_SAMPLERS, NUM_SPINS], 0, 2, dtype=tf.int32)*2-1
    states = tf.cast(states, tf.int8)
    states_shaped = tf.reshape(states, (NUM_SAMPLERS,)+SYSTEM_SHAPE)
    factors = tf.reshape(
        factors_op(pad(states_shaped, (K-1)//2)),
        (NUM_SAMPLERS, -1))

    return tf.group(
        tf.assign(current_samples, states),
        tf.assign(current_factors, factors),
        tf.assign(samples, tf.zeros_like(samples, dtype=samples.dtype)),
        tf.assign(flip_positions, tf.random_uniform(
            [TOTAL_ITS, NUM_SAMPLERS], 0, NUM_SPINS, dtype=tf.int32)),
    )


def mcmc_step(i):
    """Do MCMC Step."""
    with tf.variable_scope('sampler', reuse=True):
        current_samples = tf.get_variable('current_samples', dtype=tf.int8)
        current_factors = tf.get_variable('current_factors',
                                          dtype=tf.complex64)
        samples = tf.get_variable('samples', dtype=tf.int8)
        flip_positions = tf.get_variable('flip_positions', dtype=tf.int32)

    centers = flip_positions[i]
    setter = tf.cast(np.ones(NUM_SAMPLERS) * i, tf.int8)
    indices = tf.stack((np.arange(NUM_SAMPLERS), centers), 1)
    current_samples = tf.scatter_nd_update(current_samples, indices, setter)

    def write_samples():
        """Write current_samples to samples."""
        with tf.variable_scope('sampler', reuse=True):
            current_samples = tf.get_variable('current_samples', dtype=tf.int8)
            samples = tf.get_variable('samples', dtype=tf.int8)
        j = (i - THERM_ITS)//ITS_PER_SAMPLE
        return tf.scatter_update(samples, j, current_samples)

    with tf.control_dependencies([current_samples]):
        write_op = tf.cond(
            tf.logical_and(
                tf.greater_equal(i, THERM_ITS),
                tf.equal((i - THERM_ITS) % ITS_PER_SAMPLE, 0)),
            write_samples, lambda: samples)

    with tf.control_dependencies([write_op]):
        return i+1


def mcmc_op():
    """Get MCMC Samples."""
    with tf.control_dependencies([mcmc_reset()]):
        loop = tf.while_loop(
            lambda i: i < TOTAL_ITS,
            mcmc_step,
            [tf.constant(0)],
            parallel_iterations=1,
            back_prop=False
        )
    # with tf.control_dependencies([mcmc_reset()]):
    with tf.control_dependencies([loop]):
        with tf.variable_scope('sampler', reuse=True):
            samples = tf.get_variable('samples', dtype=tf.int8)
        return tf.identity(samples)


with tf.Graph().as_default(), tf.Session() as sess:
    create_vars()
    sess.run(tf.global_variables_initializer())

    # x = tf.random_uniform((BATCH_SIZE, NUM_SPINS), 0, 2, dtype=tf.int32)*2-1
    # x = tf.cast(x, tf.int8)
    # op = factors_op(x)
    op = mcmc_op()
    print(sess.run(op))
