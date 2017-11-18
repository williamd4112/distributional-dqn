import numpy as np
import os
import dill
import tempfile
import tensorflow as tf
import zipfile

import baselines.common.tf_util as U

from baselines import logger
from baselines.common.schedules import LinearSchedule
import distdeepq
from distdeepq.replay_buffer import ReplayBuffer, PrioritizedReplayBuffer


class ActWrapper(object):
    def __init__(self, act, act_params):
        self._act = act
        self._act_params = act_params

    @staticmethod
    def load(path, num_cpu=16):
        with open(path, "rb") as f:
            model_data, act_params = dill.load(f)
        act = distdeepq.build_act(**act_params)
        sess = U.make_session(num_cpu=num_cpu)
        sess.__enter__()
        with tempfile.TemporaryDirectory() as td:
            arc_path = os.path.join(td, "packed.zip")
            with open(arc_path, "wb") as f:
                f.write(model_data)

            zipfile.ZipFile(arc_path, 'r', zipfile.ZIP_DEFLATED).extractall(td)
            U.load_state(os.path.join(td, "model"))

        return ActWrapper(act, act_params)

    def __call__(self, *args, **kwargs):
        return self._act(*args, **kwargs)

    def save(self, path):
        """Save model to a pickle located at `path`"""
        with tempfile.TemporaryDirectory() as td:
            U.save_state(os.path.join(td, "model"))
            arc_name = os.path.join(td, "packed.zip")
            with zipfile.ZipFile(arc_name, 'w') as zipf:
                for root, dirs, files in os.walk(td):
                    for fname in files:
                        file_path = os.path.join(root, fname)
                        if file_path != arc_name:
                            zipf.write(file_path, os.path.relpath(file_path, td))
            with open(arc_name, "rb") as f:
                model_data = f.read()
        with open(path, "wb") as f:
            dill.dump((model_data, self._act_params), f)

    def get_dist_params(self):
        return self._act_params['dist_params']


def load(path, num_cpu=16):
    """Load act function that was returned by learn function.

    Parameters
    ----------
    path: str
        path to the act function pickle
    num_cpu: int
        number of cpus to use for executing the policy

    Returns
    -------
    act: ActWrapper
        function that takes a batch of observations
        and returns actions.
    """
    return ActWrapper.load(path, num_cpu=num_cpu)


def make_session(num_cpu):
    tf_config = tf.ConfigProto(
        inter_op_parallelism_threads=num_cpu,
        intra_op_parallelism_threads=num_cpu)
    tf_config.gpu_options.per_process_gpu_memory_fraction = 0.25
    return tf.Session(config=tf_config)


def learn(env,
          quant_func,
          lr=5e-4,
          max_timesteps=100000,
          buffer_size=50000,
          exploration_fraction=0.1,
          exploration_final_eps=0.01,
          train_freq=1,
          batch_size=32,
          print_freq=1,
          checkpoint_freq=10000,
          learning_starts=1000,
          gamma=0.95,
          target_network_update_freq=500,
          prioritized_replay=False,
          prioritized_replay_alpha=0.6,
          prioritized_replay_beta0=0.4,
          prioritized_replay_beta_iters=None,
          prioritized_replay_eps=1e-6,
          num_cpu=4,
          param_noise=False,
          callback=None,
          dist_params=None
          ):
    """Train a distdeepq model.

    Parameters
    -------
    env: gym.Env
        environment to train on
    quant_func: (tf.Variable, int, str, bool) -> tf.Variable
        the model that takes the following inputs:
            observation_in: object
                the output of observation placeholder
            num_actions: int
                number of actions
            scope: str
            reuse: bool
                should be passed to outer variable scope
        and returns a tensor of shape (batch_size, num_actions) with values of every action.
    lr: float
        learning rate for adam optimizer
    max_timesteps: int
        number of env steps to optimizer for
    buffer_size: int
        size of the replay buffer
    exploration_fraction: float
        fraction of entire training period over which the exploration rate is annealed
    exploration_final_eps: float
        final value of random action probability
    train_freq: int
        update the model every `train_freq` steps.
        set to None to disable printing
    batch_size: int
        size of a batched sampled from replay buffer for training
    print_freq: int
        how often to print out training progress
        set to None to disable printing
    checkpoint_freq: int
        how often to save the model. This is so that the best version is restored
        at the end of the training. If you do not wish to restore the best version at
        the end of the training set this variable to None.
    learning_starts: int
        how many steps of the model to collect transitions for before learning starts
    gamma: float
        discount factor
    target_network_update_freq: int
        update the target network every `target_network_update_freq` steps.
    prioritized_replay: True
        if True prioritized replay buffer will be used.
    prioritized_replay_alpha: float
        alpha parameter for prioritized replay buffer
    prioritized_replay_beta0: float
        initial value of beta for prioritized replay buffer
    prioritized_replay_beta_iters: int
        number of iterations over which beta will be annealed from initial value
        to 1.0. If set to None equals to max_timesteps.
    prioritized_replay_eps: float
        epsilon to add to the TD errors when updating priorities.
    num_cpu: int
        number of cpus to use for training
    callback: (locals, globals) -> None
        function called at every steps with state of the algorithm.
        If callback returns true training stops.

    Returns
    -------
    act: ActWrapper
        Wrapper over act function. Adds ability to save it and load it.
        See header of baselines/distdeepq/categorical.py for details on the act function.
    """
    # Create all the functions necessary to train the model

    sess = make_session(num_cpu=num_cpu)
    sess.__enter__()

    def make_obs_ph(name):
        return U.BatchInput(env.observation_space.shape, name=name)

    if dist_params is None:
        raise ValueError('dist_params is required')

    act, train, update_target, debug = distdeepq.build_train(
        make_obs_ph=make_obs_ph,
        quant_func=quant_func,
        num_actions=env.action_space.n,
        # optimizer=tf.train.AdamOptimizer(learning_rate=lr, epsilon=0.01/32),
        optimizer=tf.train.AdamOptimizer(learning_rate=lr),
        gamma=gamma,
        param_noise=param_noise,
        dist_params=dist_params
    )

    act_params = {
        'make_obs_ph': make_obs_ph,
        'p_dist_func': quant_func,
        'num_actions': env.action_space.n,
        'dist_params': dist_params
    }

    # Create the replay buffer
    if prioritized_replay:
        replay_buffer = PrioritizedReplayBuffer(buffer_size, alpha=prioritized_replay_alpha)
        if prioritized_replay_beta_iters is None:
            prioritized_replay_beta_iters = max_timesteps
        beta_schedule = LinearSchedule(prioritized_replay_beta_iters,
                                       initial_p=prioritized_replay_beta0,
                                       final_p=1.0)
    else:
        replay_buffer = ReplayBuffer(buffer_size)
        beta_schedule = None
    # Create the schedule for exploration starting from 1.
    exploration = LinearSchedule(schedule_timesteps=int(exploration_fraction * max_timesteps),
                                 initial_p=1.0,
                                 final_p=exploration_final_eps)

    # Initialize the parameters and copy them to the target network.
    U.initialize()
    update_target()

    episode_rewards = [0.0]
    saved_mean_reward = None
    obs = env.reset()
    reset = True
    # init_w = [x for x in tf.trainable_variables() if x.name == 'distdeepq/target_q_func/action_value/fully_connected_1/weights:0']
    # w = sess.run(init_w[0])
    # print(tf.trainable_variables())
    # print(np.mean(w), np.std(w))
    with tempfile.TemporaryDirectory() as td:
        model_saved = False
        model_file = os.path.join(td, "model")
        for t in range(max_timesteps):
            if callback is not None:
                if callback(locals(), globals()):
                    break
            # Take action and update exploration to the newest value
            kwargs = {}
            if not param_noise:
                update_eps = exploration.value(t)
                update_param_noise_threshold = 0.
            else:
                update_eps = 0.
                # Compute the threshold such that the KL divergence between perturbed and non-perturbed
                # policy is comparable to eps-greedy exploration with eps = exploration.value(t).
                # See Appendix C.1 in Parameter Space Noise for Exploration, Plappert et al., 2017
                # for detailed explanation.
                update_param_noise_threshold = -np.log(1. - exploration.value(t) + exploration.value(t) / float(env.action_space.n))
                kwargs['reset'] = reset
                kwargs['update_param_noise_threshold'] = update_param_noise_threshold
                kwargs['update_param_noise_scale'] = True
            # print(debug['quant_values'](np.ones_like(np.array(obs)[None])))
            # print(debug['quant_values'](np.array(obs)[None]))
            # print(np.array(obs)[None])
            # quit()
            action = act(np.array(obs)[None], update_eps=update_eps, **kwargs)[0]
            reset = False
            new_obs, rew, done, _ = env.step(action)

            # Store transition in the replay buffer.
            replay_buffer.add(obs, action, rew, new_obs, float(done))
            obs = new_obs

            episode_rewards[-1] += rew
            if done:
                obs = env.reset()
                episode_rewards.append(0.0)
                reset = True

            if t > learning_starts and t % train_freq == 0:
                # Minimize the error in Bellman's equation on a batch sampled from replay buffer.
                if prioritized_replay:
                    experience = replay_buffer.sample(batch_size, beta=beta_schedule.value(t))
                    (obses_t, actions, rewards, obses_tp1, dones, weights, batch_idxes) = experience
                else:
                    obses_t, actions, rewards, obses_tp1, dones = replay_buffer.sample(batch_size)
                    weights, batch_idxes = np.ones_like(rewards), None

                # ===========================================================================

                # if -1 in rewards:
                # if True:
                if t % 10000 == 0:
                    inputs = {'distdeepq_1/obs_t:0': obses_t, 'distdeepq_1/obs_tp1:0': obses_tp1,
                              'distdeepq_1/action:0': actions, 'distdeepq_1/reward:0': rewards,
                              'distdeepq_1/done:0': dones}
                    print('--quant_t_selected')
                    print(sess.run(debug['quant_t_selected'], inputs))
                    print('--quant_tp1_star')
                    print(sess.run(debug['quant_tp1_star'], inputs))

                    # print(rewards[0] + gamma * sess.run(debug['quant_tp1'], inputs)[0])
                    # print(sess.run(debug['quant_selected'], inputs)[0])
                    print('--quant_target')
                    print(sess.run(debug['quant_target'], inputs))
                    print(actions)
                    print(rewards)
                    print(sess.run(debug['a_star'], inputs))
                    print('----')
                    # print(sess.run(debug['quant_masked'], inputs))
                    # print(sess.run(debug['tau_hat'], inputs))
                    # print(sess.run(debug['negative_indicator'], inputs))
                    # print(sess.run(debug['quant_weights'], inputs))
                    # print(sess.run(debug['quant_target'], inputs))
                    # print(sess.run(debug['big_quant_target'], inputs))
                    # errors = train(obses_t, actions, rewards, obses_tp1, dones, weights)

                    # print('--------------')
                    # print(sess.run(debug['quant_t_selected'], inputs)[0])
                    # quit()
                    # ===========================================================================

                errors = train(obses_t, actions, rewards, obses_tp1, dones, weights)

                if prioritized_replay:
                    new_priorities = np.abs(errors) + prioritized_replay_eps
                    replay_buffer.update_priorities(batch_idxes, new_priorities)

            if t > learning_starts and t % target_network_update_freq == 0:
                # Update target network periodically.
                update_target()

            mean_100ep_reward = round(np.mean(episode_rewards[-101:-1]), 1)
            num_episodes = len(episode_rewards)
            if done and print_freq is not None and len(episode_rewards) % print_freq == 0:
                logger.record_tabular("steps", t)
                logger.record_tabular("episodes", num_episodes)
                logger.record_tabular("mean 100 episode reward", mean_100ep_reward)
                logger.record_tabular("% time spent exploring", int(100 * exploration.value(t)))
                logger.dump_tabular()

            if (checkpoint_freq is not None and t > learning_starts and
                    num_episodes > 100 and t % checkpoint_freq == 0):
                if saved_mean_reward is None or mean_100ep_reward > saved_mean_reward:
                    if print_freq is not None:
                        logger.log("Saving model due to mean reward increase: {} -> {}".format(
                                   saved_mean_reward, mean_100ep_reward))
                    U.save_state(model_file)
                    model_saved = True
                    saved_mean_reward = mean_100ep_reward
        if model_saved:
            if print_freq is not None:
                logger.log("Restored model with mean reward: {}".format(saved_mean_reward))
            U.load_state(model_file)

    return ActWrapper(act, act_params)
