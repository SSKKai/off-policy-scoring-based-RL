import argparse
import time
from collections import deque
import numpy as np
import itertools
import torch
from sac import SAC
from utils import get_wandb_config, set_seeds
from replay_memory import ReplayMemory
from reward_net import RewardNetwork
import glfw

# import env
import gym  # mujoco
from custom_env import CustomEnv  # RLBench
import metaworld.envs.mujoco.env_dict as _env_dict  # metaworld

# import config and logger
import hydra
import wandb


class OPRRL(object):
    def __init__(self, config):

        # Configurations
        self.sac_hyparams = config.sac
        self.reward_hyparams = config.reward
        self.env_config = config.env

        # Experiment setup
        self.episode_len = config.experiment.episode_len
        self.max_episodes = config.experiment.max_episodes
        self.seeds = config.experiment.seed
        self.change_flag_reward = config.experiment.change_flag_reward

        # Environment
        self.env_type = config.experiment.env_type

        if self.env_type == "rlbench":
            self.env = CustomEnv(self.env_config)
            self.env.reset()

        elif self.env_type == "mujoco":
            if self.env_config.terminate_when_unhealthy is None:
                self.env = gym.make(self.env_config.task)
            else:
                self.env = gym.make(self.env_config.task, terminate_when_unhealthy=self.env_config.terminate_when_unhealthy)
            self.env._max_episode_steps = self.episode_len
            self.env.seed(self.seeds)
            self.env.action_space.seed(self.seeds)

        elif self.env_type == 'metaworld':
            env_cls = _env_dict.ALL_V2_ENVIRONMENTS[self.env_config.task]
            self.env = env_cls()
            self.env._freeze_rand_vec = False
            self.env._set_task_called = True
            self.env.seed(self.seeds)
            self.env.action_space.seed(self.seeds)
            self.env.max_path_length = self.episode_len

        else:
            raise Exception('wrong environment type, available: rlbench/mujoco/metaworld')

        set_seeds(self.seeds)
        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space

        
        # Agent
        self.agent = SAC(self.state_dim, self.action_dim, args=self.sac_hyparams)
        
        # Memory
        self.agent_memory = ReplayMemory(self.sac_hyparams.replay_size, self.seeds, self.reward_hyparams.state_only)
        
        # Reward Net
        self.reward_network = RewardNetwork(self.state_dim, self.action_dim.shape[0], self.episode_len, self.env_type, args=self.reward_hyparams)

        # wandb logger
        self.wandb_log = config.experiment.wandb_log
        if self.wandb_log:
            config_wandb = get_wandb_config(config)
            self.logger = wandb.init(config = config, project='oprrl_'+config.experiment.env_type+'_'+config.env.task+'_sampling')
            self.logger.config.update(config_wandb)
            # self.logger.watch(self.agent.policy)
            # self.logger.watch(self.reward_network.reward_network)
        
        # Training Loop
        self.total_numsteps = 0
        self.updates = 0
        self.rank_count = 0
        self.rank_num = 0

        self.start_episodes = self.sac_hyparams.start_episodes
        self.pretrain_episodes = self.sac_hyparams.pretrain_episodes
        
        self.reward_list = []
        self.reward_prime_list = []
        self.e_reward_list = []
        self.e_reward_prime_list = []
        self.episode_len_list = []
        
        self.training_flag = 2

        
    def evaluate(self, i_episode=20, episode_len=250, evaluate_mode=True):
        print("----------------------------------------")
        total_reward = []
        for _ in range(i_episode):
            state = self.env.reset()
            episode_reward = 0
            episode_reward_prime = 0
            done = False
            episode_steps = 0
            while not done:
                if self.env_type == "mujoco" or self.env_type == 'metaworld':
                    if self.env_config.render:
                        self.env.render()
                action = self.agent.select_action(state, evaluate=evaluate_mode)
                next_state, reward, done, _ = self.env.step(action)
                # reward_prime = self.reward_network.get_reward(state, action).detach().cpu().numpy()[0]
                episode_reward += reward
                state = next_state
                episode_steps += 1
                if episode_steps == episode_len:
                    done = True
            print("Reward: {}".format(round(episode_reward, 2)))
            total_reward.append(episode_reward)

        print("----------------------------------------")
        return np.array(total_reward)

    def learn_reward(self, num_learn_reward=8, early_break=True, relabel_memory=True):

        acc_ls = []
        if self.rank_count >= 5:
            for i in range(num_learn_reward):  # 5
                acc = self.reward_network.learn_reward_soft()
                acc_ls.append(acc)
                if early_break and acc > 0.97:
                    break
        else:
            acc = self.reward_network.learn_reward_soft()
            acc_ls.append(acc)
        acc = np.mean(acc_ls)

        if relabel_memory:
            self.agent_memory.relabel_memory(self.reward_network)

        return acc

    def train(self):

        frequency_flag = 1
        reach_count = 0
        succ_de = deque(maxlen=50)
        max_succ_rate = 0
        extra_update_reward_flag = 0
        print_flag = 0

        for self.i_episode in itertools.count(1):
            episode_reward = 0
            episode_reward_prime = 0
            episode_steps = 0
            done = False
            state = self.env.reset()

            episode_flag = 0
            episode_succ = False

            #############################################################################
            ############################ step session start #############################
            #############################################################################
            while not done:
                if self.i_episode <= self.start_episodes:
                    if self.env_type == "rlbench":
                        action = self.env.randn_action()  # Sample random action
                    elif self.env_type == "mujoco" or self.env_type == 'metaworld':
                        action = self.env.action_space.sample()
                else:
                    action = self.agent.select_action(state)  # Sample action from policy
        
                if len(self.agent_memory) > self.sac_hyparams.batch_size:
                    # Number of updates per step in environment
                    for i in range(self.sac_hyparams.updates_per_step):
                        # Update parameters of all the networks
                        if self.i_episode > self.start_episodes + self.pretrain_episodes or self.pretrain_episodes == 0:
                            if print_flag == 0:
                                print('train session start')
                                print_flag += 1
                            critic_1_loss, critic_2_loss, policy_loss, ent_loss, alpha = self.agent.update_parameters(self.agent_memory, self.sac_hyparams.batch_size, self.updates)
                        else:
                            critic_1_loss, critic_2_loss, policy_loss, ent_loss, alpha = self.agent.update_parameters_pretrain(self.agent_memory, self.sac_hyparams.batch_size, self.updates)
                        self.updates += 1
                        if self.wandb_log:
                            self.logger.log({"critic_1_loss": critic_1_loss, "critic_2_loss": critic_2_loss, "policy_loss": policy_loss})

                next_state, reward, done, info = self.env.step(action)
                reward_prime = self.reward_network.get_reward(state, action).detach().cpu().numpy()[0]

                # self.env.render()

                if self.env_type == 'metaworld' and episode_flag == 0:
                    if info['success']:
                        episode_succ = True
                        episode_flag = 1
                
                episode_steps += 1
                self.total_numsteps += 1
                episode_reward += reward
                episode_reward_prime += reward_prime
        
                # Ignore the "done" signal if it comes from hitting the time horizon.
                # (https://github.com/openai/spinningup/blob/master/spinup/algos/sac/sac.py)
                mask = 1 if episode_steps == self.episode_len else float(not done)

                if episode_steps % self.episode_len == 0:
                    done = True

                self.agent_memory.push(state, action, reward_prime, next_state, mask) # push data to replay buffer
                self.reward_network.push_data(state, action, reward, done) # push data to reward memory

                state = next_state
            #############################################################################
            ############################## step session end #############################
            #############################################################################

            if self.wandb_log:
                if self.env_type == "mujoco":
                    self.logger.log({"e_reward": episode_reward, "e_reward_prime": episode_reward_prime, "episode_steps": episode_steps, "i_episode": self.i_episode})
                else:
                    if self.env_type == "rlbench":
                        task_succ = True if episode_steps < self.episode_len else False

                    elif self.env_type == 'metaworld':
                        task_succ = episode_succ

                    succ_de.append(task_succ)
                    succ_rate = np.mean(succ_de)
                    self.logger.log({"e_reward": episode_reward, "e_reward_prime": episode_reward_prime, "episode_steps": episode_steps, "success_rate": succ_rate, "i_episode": self.i_episode})

                    # max_succ_rate = max(max_succ_rate, succ_rate)
                    # if max_succ_rate - succ_rate > 0.3:
                    #     extra_update_reward_flag += 1
                    #     if extra_update_reward_flag > 10:
                    #         self.learn_reward(num_learn_reward=20, early_break=False)
                    #         extra_update_reward_flag = 0


            self.e_reward_list.append(episode_reward)
            self.e_reward_prime_list.append(episode_reward_prime)
            self.episode_len_list.append(episode_steps)
            print("E{}, t numsteps: {}, e steps: {}, reward: {}, reward_prime: {}".format(self.i_episode, self.total_numsteps, episode_steps,
                                                                                                            round(episode_reward, 2),round(episode_reward_prime, 2)))



            if frequency_flag == 1:
                learn_frequency = self.reward_hyparams.learn_reward_frequency_1
                num_to_rank = self.reward_hyparams.num_to_rank_1
                if episode_reward > self.change_flag_reward:
                    reach_count += 1
                if reach_count > 8:
                    frequency_flag = 2
            
            if frequency_flag == 2:
                learn_frequency = self.reward_hyparams.learn_reward_frequency
                num_to_rank = self.reward_hyparams.num_to_rank
            
            # learn reward
            if self.i_episode % learn_frequency == 0 and self.rank_num <= self.reward_hyparams.max_rank_num:
                k_tau = self.reward_network.rank(num_to_rank)
                self.rank_count += 1
                self.rank_num = len(self.reward_network.ranked_trajs)
                print('rank successfully')

                if frequency_flag == 1:
                    acc = self.learn_reward(num_learn_reward=8, early_break=False)

                if frequency_flag == 2:
                    acc = self.learn_reward(num_learn_reward=self.rank_count, early_break=True)

                if self.wandb_log:
                    self.logger.log({'acc': acc, 'k_tau': k_tau, 'rank_count': self.rank_count, 'rank_num': self.rank_num})



            if self.i_episode == self.start_episodes + self.pretrain_episodes and self.pretrain_episodes > 0:
                for _ in range(10):
                    self.reward_network.learn_reward_soft()
                self.agent_memory.relabel_memory(self.reward_network)

                self.agent.reset_critic()
                self.agent.reset_actor()
                for _ in range(100):
                    self.agent.update_parameters(self.agent_memory, self.sac_hyparams.batch_size, self.updates)
                print('pretrain session end')

            if self.i_episode % self.sac_hyparams.eval_per_episode == 0 and self.sac_hyparams.eval is True:
                self.evaluate(self.sac_hyparams.eval_episodes, self.episode_len)
            
            if self.i_episode >= self.max_episodes - 1:
                break
        # self.env.close()


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--config-name', default="mujoco-HalfCheetah-fb250",
                        help='Please specify the config name (default: mujoco-HalfCheetah-fb250)')
    args = parser.parse_args()

    with hydra.initialize(config_path="config"):
        config = hydra.compose(config_name=args.config_name)
    print(config.experiment.description)

    oprrl = OPRRL(config)
    oprrl.train()



