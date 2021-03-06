#!/usr/bin/env python
# -*- coding: utf8 -*-

import sys
import numpy as np
import tensorflow as tf
import logging
import argparse

import gym
from gym import wrappers
from gym.spaces import Discrete, Box

from Learner import Learner
from utils import discount_rewards
from Reporter import Reporter
from ActionSelection import ProbabilisticCategoricalActionSelection, ContinuousActionSelection

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

np.set_printoptions(suppress=True)  # Don't use the scientific notation to print results

class A2C(Learner):
    """Advantage Actor Critic"""
    def __init__(self, env, action_selection, monitor_dir, **usercfg):
        super(A2C, self).__init__(env, **usercfg)
        self.action_selection = action_selection
        self.monitor_dir = monitor_dir

        self.config = dict(
            episode_max_length=env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps'),
            timesteps_per_batch=2000,
            trajectories_per_batch=10,
            batch_update="timesteps",
            n_iter=400,
            gamma=0.99,
            actor_learning_rate=0.01,
            critic_learning_rate=0.05,
            actor_n_hidden=20,
            critic_n_hidden=20,
            repeat_n_actions=1
        )
        self.config.update(usercfg)
        self.build_networks()
        self.rewards = tf.placeholder("float", name="Rewards")
        self.episode_lengths = tf.placeholder("float", name="Episode_lengths")
        summary_actor_loss = tf.summary.scalar("Actor_loss", self.summary_actor_loss)
        summary_critic_loss = tf.summary.scalar("Critic_loss", self.summary_critic_loss)
        summary_rewards = tf.summary.scalar("Rewards", self.rewards)
        summary_episode_lengths = tf.summary.scalar("Episode_lengths", self.episode_lengths)
        self.summary_op = tf.summary.merge([summary_actor_loss, summary_critic_loss, summary_rewards, summary_episode_lengths])
        self.writer = tf.summary.FileWriter(self.monitor_dir + '/summaries', self.sess.graph)
        return

    def get_critic_value(self, state):
        return self.sess.run([self.critic_value], feed_dict={self.critic_state_in: state})[0].flatten()

    def learn(self):
        """Run learning algorithm"""
        reporter = Reporter()
        config = self.config
        possible_actions = np.arange(self.nA)
        total_n_trajectories = 0
        for iteration in range(config["n_iter"]):
            # Collect trajectories until we get timesteps_per_batch total timesteps
            trajectories = self.get_trajectories()
            total_n_trajectories += len(trajectories)
            all_action = np.concatenate([trajectory["action"] for trajectory in trajectories])
            all_action = (possible_actions == all_action[:, None]).astype(np.float32)
            all_state = np.concatenate([trajectory["state"] for trajectory in trajectories])
            # Compute discounted sums of rewards
            returns = np.concatenate([discount_rewards(trajectory["reward"], config["gamma"]) for trajectory in trajectories])
            qw_new = self.get_critic_value(all_state)

            episode_rewards = np.array([trajectory["reward"].sum() for trajectory in trajectories])  # episode total rewards
            episode_lengths = np.array([len(trajectory["reward"]) for trajectory in trajectories])  # episode lengths

            results = self.sess.run([self.summary_op, self.critic_train, self.actor_train], feed_dict={
                                    self.critic_state_in: all_state,
                                    self.critic_target: returns,
                                    self.actor_input: all_state,
                                    self.actions_taken: all_action,
                                    self.critic_feedback: qw_new,
                                    self.critic_rewards: returns,
                                    self.rewards: np.mean(episode_rewards),
                                    self.episode_lengths: np.mean(episode_lengths)
                                    })
            self.writer.add_summary(results[0], iteration)
            self.writer.flush()

            reporter.print_iteration_stats(iteration, episode_rewards, episode_lengths, total_n_trajectories)

class A2CDiscrete(A2C):
    """A2C learner for a discrete action space"""
    def __init__(self, env, action_selection, monitor_dir, **usercfg):
        self.nA = env.action_space.n
        super(A2CDiscrete, self).__init__(env, action_selection, monitor_dir, **usercfg)

    def build_networks(self):
        self.actor_input = tf.placeholder(tf.float32, name='actor_input')
        self.actions_taken = tf.placeholder(tf.float32, name='actions_taken')
        self.critic_feedback = tf.placeholder(tf.float32, name='critic_feedback')
        self.critic_rewards = tf.placeholder(tf.float32, name='critic_rewards')

        # Actor network
        W0 = tf.Variable(tf.random_normal([self.nO, self.config['actor_n_hidden']]), name='W0')
        b0 = tf.Variable(tf.zeros([self.config['actor_n_hidden']]), name='b0')
        L1 = tf.tanh(tf.matmul(self.actor_input, W0) + b0[None, :], name='L1')

        W1 = tf.Variable(tf.random_normal([self.config['actor_n_hidden'], self.nA]), name='W1')
        b1 = tf.Variable(tf.zeros([self.nA]), name='b1')
        self.prob_na = tf.nn.softmax(tf.matmul(L1, W1) + b1[None, :], name='prob_na')

        good_probabilities = tf.reduce_sum(tf.multiply(self.prob_na, self.actions_taken), reduction_indices=[1])
        eligibility = tf.log(tf.where(tf.equal(good_probabilities, tf.fill(tf.shape(good_probabilities), 0.0)), tf.fill(tf.shape(good_probabilities), 1e-30), good_probabilities)) \
            * (self.critic_rewards - self.critic_feedback)
        loss = -tf.reduce_mean(eligibility)
        # loss = tf.Print(loss, [loss], message='Actor loss=')
        self.summary_actor_loss = loss
        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.config['actor_learning_rate'])
        self.actor_train = self.optimizer.minimize(loss, global_step=tf.contrib.framework.get_global_step())

        self.critic_state_in = tf.placeholder("float", [None, self.nO], name='critic_state_in')
        self.critic_target = tf.placeholder("float", name="critic_target")

        # Critic network
        critic_W0 = tf.Variable(tf.random_normal([self.nO, self.config['critic_n_hidden']]), name='W0')
        critic_b0 = tf.Variable(tf.zeros([self.config['actor_n_hidden']]), name='b0')
        critic_L1 = tf.tanh(tf.matmul(self.critic_state_in, critic_W0) + critic_b0[None, :], name='L1')

        critic_W1 = tf.Variable(tf.random_normal([self.config['actor_n_hidden'], 1]), name='W1')
        critic_b1 = tf.Variable(tf.zeros([1]), name='b1')
        self.critic_value = tf.matmul(critic_L1, critic_W1) + critic_b1[None, :]
        critic_loss = tf.reduce_mean(tf.square(self.critic_target - self.critic_value))
        # critic_loss = tf.Print(critic_loss, [critic_loss], message='Critic loss=')
        self.summary_critic_loss = critic_loss
        critic_optimizer = tf.train.AdamOptimizer(learning_rate=self.config['critic_learning_rate'])
        self.critic_train = critic_optimizer.minimize(critic_loss, global_step=tf.contrib.framework.get_global_step())

        init = tf.global_variables_initializer()

        # Launch the graph.
        self.sess = tf.Session()
        self.sess.run(init)

    def choose_action(self, state):
        """Choose an action."""
        state = state.reshape(1, -1)
        prob = self.sess.run([self.prob_na], feed_dict={self.actor_input: state})[0][0]
        action = self.action_selection.select_action(prob)
        return action

class A2CContinuous(A2C):
    """Advantage Actor Critic for continuous action spaces."""
    def __init__(self, env, action_selection, monitor_dir, **usercfg):
        super(A2CContinuous, self).__init__(env, action_selection, monitor_dir, **usercfg)

    def build_networks(self):
        self.input_state = tf.placeholder(tf.float32, [None, self.ob_space.shape[0]], name='input_state')
        self.actions_taken = tf.placeholder(tf.float32, name='actions_taken')
        self.critic_feedback = tf.placeholder(tf.float32, name='critic_feedback')
        self.critic_rewards = tf.placeholder(tf.float32, name='critic_rewards')

        mu = tf.contrib.layers.fully_connected(
            inputs=tf.expand_dims(self.input_state, 0),
            num_outputs=1,
            activation_fn=None,
            weights_initializer=tf.zeros_initializer())
        mu = tf.squeeze(mu)

        sigma = tf.contrib.layers.fully_connected(
            inputs=tf.expand_dims(self.input_state, 0),
            num_outputs=1,
            activation_fn=None,
            weights_initializer=tf.zeros_initializer())
        sigma = tf.squeeze(sigma)
        sigma = tf.nn.softplus(sigma) + 1e-5

        self.normal_dist = tf.contrib.distributions.Normal(mu, sigma)
        self.action = self.normal_dist.sample_n(1)
        self.action = tf.clip_by_value(self.action, self.action_space.low[0], self.action_space.high[0])

        # Loss and train op
        self.loss = -self.normal_dist.log_prob(tf.squeeze(self.actions_taken)) * (self.critic_rewards - self.critic_feedback)
        # Add cross entropy cost to encourage exploration
        self.loss -= 1e-1 * self.normal_dist.entropy()
        self.summary_actor_loss = tf.reduce_mean(self.loss)

        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.config['actor_learning_rate'])
        self.actor_train = self.optimizer.minimize(
            self.loss, global_step=tf.contrib.framework.get_global_step())

        self.critic_state_in = tf.placeholder("float", [None, self.nO], name='critic_state_in')
        self.critic_target = tf.placeholder("float", name="critic_target")

        # Critic network
        critic_W0 = tf.Variable(tf.zeros([self.nO, self.config['critic_n_hidden']]), name='W0')
        critic_b0 = tf.Variable(tf.zeros([self.config['actor_n_hidden']]), name='b0')
        critic_L1 = tf.tanh(tf.matmul(self.critic_state_in, critic_W0) + critic_b0[None, :], name='L1')

        critic_W1 = tf.Variable(tf.zeros([self.config['actor_n_hidden'], 1]), name='W1')
        critic_b1 = tf.Variable(tf.zeros([1]), name='b1')
        self.critic_value = tf.squeeze(tf.matmul(critic_L1, critic_W1) + critic_b1[None, :])

        critic_loss = tf.reduce_mean(tf.squared_difference(self.critic_target, self.critic_value))
        # critic_loss = tf.Print(critic_loss, [critic_loss], message='Critic loss=')
        self.summary_critic_loss = critic_loss
        critic_optimizer = tf.train.AdamOptimizer(learning_rate=self.config['critic_learning_rate'])
        self.critic_train = critic_optimizer.minimize(critic_loss, global_step=tf.contrib.framework.get_global_step())

        init = tf.global_variables_initializer()

        # Launch the graph.
        self.sess = tf.Session()
        self.sess.run(init)

    def choose_action(self, state):
        """Choose an action."""
        return self.sess.run([self.action], feed_dict={self.input_state: [state]})[0]

    def learn(self):
        """Run learning algorithm"""
        reporter = Reporter()
        config = self.config
        total_n_trajectories = 0
        for iteration in range(config["n_iter"]):
            # Collect trajectories until we get timesteps_per_batch total timesteps
            trajectories = self.get_trajectories()
            total_n_trajectories += len(trajectories)
            all_action = np.concatenate([trajectory["action"] for trajectory in trajectories])
            all_state = np.concatenate([trajectory["state"] for trajectory in trajectories])
            # Compute discounted sums of rewards
            returns = np.concatenate([discount_rewards(trajectory["reward"], config["gamma"]) for trajectory in trajectories])
            qw_new = self.get_critic_value(all_state)

            episode_rewards = np.array([trajectory["reward"].sum() for trajectory in trajectories])  # episode total rewards
            episode_lengths = np.array([len(trajectory["reward"]) for trajectory in trajectories])  # episode lengths

            results = self.sess.run([self.summary_op, self.critic_train, self.actor_train], feed_dict={
                                    self.critic_state_in: all_state,
                                    self.critic_target: returns,
                                    self.input_state: all_state,
                                    self.actions_taken: all_action,
                                    self.critic_feedback: qw_new,
                                    self.critic_rewards: returns,
                                    self.rewards: np.mean(episode_rewards),
                                    self.episode_lengths: np.mean(episode_lengths)
                                    })
            self.writer.add_summary(results[0], iteration)
            self.writer.flush()

            reporter.print_iteration_stats(iteration, episode_rewards, episode_lengths, total_n_trajectories)
            # get_trajectory(self, env, config["episode_max_length"], render=True)

parser = argparse.ArgumentParser()
parser.add_argument("environment", metavar="env", type=str, help="Gym environment to execute the experiment on.")
parser.add_argument("monitor_path", metavar="monitor_path", type=str, help="Path where Gym monitor files may be saved")

def main():
    try:
        args = parser.parse_args()
    except:
        sys.exit()
    env = gym.make(args.environment)
    if isinstance(env.action_space, Discrete):
        action_selection = ProbabilisticCategoricalActionSelection()
        agent = A2CDiscrete(env, action_selection, args.monitor_path)
    elif isinstance(env.action_space, Box):
        action_selection = ContinuousActionSelection()
        agent = A2CContinuous(env, action_selection, args.monitor_path)
    else:
        raise NotImplementedError
    try:
        agent.env = wrappers.Monitor(agent.env, args.monitor_path, force=True)
        agent.learn()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
