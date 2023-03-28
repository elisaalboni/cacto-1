import sys
import math
import time
import numpy as np
import tensorflow as tf
from pyomo.dae import *
from pyomo.environ import *
from tensorflow.keras import layers, regularizers
from CACTO import CACTO


class RL_AC(CACTO):
    def __init__(self, env, conf):
        super(RL_AC, self).__init__(env, conf, init_setup_model=False)

        return

    def update(self, episode, red_state_batch_norm, red_state_batch, state_batch_norm, state_batch, reward_batch, state_next_batch, state_next_batch_norm, d_batch, action_batch, weights_batch=None):
        ''' Update both critic and actor '''
        # Update the critic
        with tf.GradientTape() as tape:
            if self.conf.SOBOLEV:
                if self.conf.NORMALIZE_INPUTS:
                    with tf.GradientTape() as tape3:
                        tape3.watch(state_batch_norm)
                        s_batch = self.env.de_normalize(state_batch_norm, self.conf.BATCH_SIZE)
                        state_next_tf = self.env.simulate_tf(s_batch,action_batch)      
                        rewards_tf = self.env.reward_tf(state_next_tf, action_batch, self.conf.BATCH_SIZE, d_batch)    # We need also the batch of rewards associated to the batches of next states and actions
                        target_values = CACTO.target_critic(state_next_tf, training=True)
                        y = rewards_tf + (1-d_batch)*(target_values)
                        target_grad = tape3.gradient(y, state_batch_norm)
                    with tf.GradientTape() as tape2:
                        tape2.watch(state_batch_norm)
                        critic_value = CACTO.critic_model(state_batch_norm, training=True)                    # Compute batch of Values associated to the sampled batch of states
                        critic_state_grad = tape2.gradient(critic_value, state_batch_norm)
                else:
                    with tf.GradientTape() as tape3:
                        tape3.watch(state_batch)
                        state_next_tf = self.env.simulate_tf(state_batch,action_batch)      
                        rewards_tf = self.env.reward_tf(state_next_tf, action_batch, self.conf.BATCH_SIZE, d_batch)    # We need also the batch of rewards associated to the batches of next states and actions
                        target_values = CACTO.target_critic(state_next_tf, training=True)
                        y = rewards_tf + (1-d_batch)*(target_values)
                        target_grad = tape3.gradient(y, state_batch)
                    with tf.GradientTape() as tape2:
                        tape2.watch(state_batch)
                        critic_value = CACTO.critic_model(state_batch, training=True)                         # Compute batch of Values associated to the sampled batch of states
                        critic_state_grad = tape2.gradient(critic_value, state_batch)
                der = critic_state_grad-target_grad
                if weights_batch is None:
                    critic_loss = tf.math.reduce_mean(tf.math.square(y - critic_value)) + self.conf.wd*tf.math.reduce_mean(tf.square(der))                              # Critic loss function (tf.math.reduce_mean computes the mean of elements across dimensions of a tensor, in this case across the batch)
                else:
                    critic_loss = tf.math.reduce_mean(tf.math.square(tf.math.multiply(weights_batch,(y - critic_value)))) + self.conf.wd*tf.math.reduce_mean(tf.square(der)) #np.clip(der,0,1e5)      # tf.math.reduce_mean computes the mean of elements across dimensions of a tensor
            else:   
                if self.conf.NORMALIZE_INPUTS:
                    if self.conf.TD_N:  
                        y = reward_batch    
                    else:
                        target_values = CACTO.target_critic(state_next_batch_norm, training=True)              # Compute Value at next state after self.conf.nsteps_TD_N steps given by target critic 
                        y = reward_batch + (1-d_batch)*target_values                                           # Compute batch of 1-step targets for the critic loss 
                    critic_value = CACTO.critic_model(state_batch_norm, training=True)                         # Compute batch of Values associated to the sampled batch of states
                else:
                    if self.conf.TD_N: 
                        y = reward_batch    
                    else:
                        target_values = CACTO.target_critic(state_next_batch, training=True)
                        y = reward_batch + (1-d_batch)*target_values   
                    critic_value = CACTO.critic_model(state_batch, training=True)
                if weights_batch is None:
                    critic_loss = tf.math.reduce_mean(tf.math.square(y - critic_value))                         # Critic loss function (tf.math.reduce_mean computes the mean of elements across dimensions of a tensor, in this case across the batch)
                else:
                    critic_loss = tf.math.reduce_mean(tf.math.square(tf.math.multiply(weights_batch,(y - critic_value))))   
        
        # Compute the gradients of the critic loss w.r.t. critic's parameters
        critic_grad = tape.gradient(critic_loss, CACTO.critic_model.trainable_variables)   

        # Update the critic backpropagating the gradients
        CACTO.critic_optimizer.apply_gradients(zip(critic_grad, CACTO.critic_model.trainable_variables))

        # Update the actor after the critic pre-training
        if episode >= self.conf.EPISODE_CRITIC_PRETRAINING:    
            if self.conf.NORMALIZE_INPUTS:
                actions = CACTO.actor_model(red_state_batch_norm, training=True)
            else:
                actions = CACTO.actor_model(red_state_batch, training=True)

            # Both take into account normalization, ds_next_da is the gradient of the dynamics w.r.t. policy actions (ds'_da)
            state_next_tf, ds_next_da = self.env.simulate_and_derivative_tf(red_state_batch,actions.numpy(),self.conf.BATCH_SIZE)
            with tf.GradientTape() as tape:
                tape.watch(state_next_tf)
                critic_value_next = CACTO.critic_model(state_next_tf, training=True)                                    # state_next_batch = next state after applying policy's action, already normalized if self.conf.NORMALIZE_INPUTS=1
            dV_ds_next = tape.gradient(critic_value_next, state_next_tf)                                                # dV_ds' = gradient of V w.r.t. s', where s'=f(s,a) a=policy(s)   
            with tf.GradientTape() as tape1:    
                with tf.GradientTape() as tape2:
                        tape1.watch(actions)
                        tape2.watch(state_next_tf)
                        rewards_tf = self.env.reward_tf(state_next_tf, actions, self.conf.BATCH_SIZE, d_batch)
            dr_da = tape1.gradient(rewards_tf,actions)                                                                  # dr_da = gradient of reward r w.r.t. policy's action a
            dr_ds_next = tape2.gradient(rewards_tf,state_next_tf)                                                       # dr_ds' = gradient of reward r w.r.t. next state s' after performing policy's action a
            dr_ds_next_dV_ds_next_reshaped = tf.reshape(dr_ds_next+dV_ds_next,(self.conf.BATCH_SIZE,1,self.conf.nb_state))        # dr_ds' + dV_ds'
            dr_ds_next_dV_ds_next = tf.matmul(dr_ds_next_dV_ds_next_reshaped,ds_next_da)                                # (dr_ds' + dV_ds')*ds'_da
            dr_da_reshaped = tf.reshape(dr_da,(self.conf.BATCH_SIZE,1,self.conf.nb_action))                                       
            tf_sum = dr_ds_next_dV_ds_next + dr_da_reshaped                                                             # (dr_ds' + dV_ds')*ds'_da + dr_da
            
            # Now let's multiply -[(dr_ds' + dV_ds')*ds'_da + dr_da] by the actions a 
            # and then let's autodifferentiate w.r.t theta_A (actor NN's parameters) to finally get -dQ/dtheta_A 
            with tf.GradientTape() as tape:
                tape.watch(CACTO.actor_model.trainable_variables)
                if self.conf.NORMALIZE_INPUTS:
                    actions = CACTO.actor_model(state_batch_norm, training=True)
                else:
                    actions = CACTO.actor_model(state_batch, training=True)
                actions_reshaped = tf.reshape(actions,(self.conf.BATCH_SIZE,self.conf.nb_action,1))
                tf_sum_reshaped = tf.reshape(tf_sum,(self.conf.BATCH_SIZE,1,self.conf.nb_action))    
                Q_neg = tf.matmul(-tf_sum_reshaped,actions_reshaped) 
                mean_Qneg = tf.math.reduce_mean(Q_neg)                                                           # Also here we need a scalar so we compute the mean -Q across the batch
            
            # Gradients of the actor loss w.r.t. actor's parameters
            actor_grad = tape.gradient(mean_Qneg, CACTO.actor_model.trainable_variables) 

            # Update the actor backpropagating the gradients
            CACTO.actor_optimizer.apply_gradients(zip(actor_grad, CACTO.actor_model.trainable_variables))

    @tf.function 
    def update_target(self,target_weights, weights, tau): 
        ''' Update target critic NN '''
        for (a, b) in zip(target_weights, weights):
            a.assign(b * tau + a * (1 - tau))  

    def learn(self, episode, prioritized_buffer):
        # Sample batch of transitions from the buffer
        experience = prioritized_buffer.sample(self.conf.BATCH_SIZE, beta=self.conf.prioritized_replay_eps)            # Bias annealing not performed, that's why beta is equal to a very small number (0 not accepted by PrioritizedReplayBuffer)
        s_batch, a_next_batch, a_batch, r_batch, s_next_batch, d_batch, weights, batch_idxes = experience        # Importance sampling weights (actually not used) should anneal the bias (see Prioritized Experience Replay paper) 
        
        # Convert batch of transitions into a tensor
        s_batch = s_batch.reshape(self.conf.BATCH_SIZE,self.conf.nb_state)
        a_next_batch = a_next_batch.reshape(self.conf.BATCH_SIZE,self.conf.nb_action)
        a_batch = a_batch.reshape(self.conf.BATCH_SIZE,self.conf.nb_action)
        r_batch = r_batch.reshape(self.conf.BATCH_SIZE,1)
        s_next_batch = s_next_batch.reshape(self.conf.BATCH_SIZE,self.conf.nb_state)
        d_batch = d_batch.reshape(self.conf.BATCH_SIZE,1)
        weights = weights.reshape(self.conf.BATCH_SIZE,1)

        state_batch = tf.convert_to_tensor(s_batch)
        action_batch = tf.convert_to_tensor(a_batch)
        reward_batch = tf.convert_to_tensor(r_batch)
        state_next_batch = tf.convert_to_tensor(s_next_batch)

        state_batch = tf.cast(state_batch, dtype=tf.float32)
        action_batch = tf.cast(action_batch, dtype=tf.float32)
        reward_batch = tf.cast(reward_batch, dtype=tf.float32)                                     
        d_batch = tf.cast(d_batch, dtype=tf.float32)
        state_next_batch = tf.cast(state_next_batch, dtype=tf.float32)  

        if self.conf.prioritized_replay_alpha == 0:
            weights_batch = tf.convert_to_tensor(np.ones_like(weights), dtype=tf.float32)
        else:    
            weights_batch = tf.convert_to_tensor(weights, dtype=tf.float32)
    
        if self.conf.NORMALIZE_INPUTS:
            s_batch_norm = s_batch / self.conf.state_norm_arr
            s_batch_norm[:,-1] = 2*s_batch_norm[:,-1] -1
            state_batch_norm = tf.convert_to_tensor(s_batch_norm)
            state_batch_norm = tf.cast(state_batch_norm, dtype=tf.float32)

            s_next_batch_norm = s_next_batch / self.conf.state_norm_arr
            s_next_batch_norm[:,-1] = 2*s_next_batch_norm[:,-1] -1
            state_next_batch_norm = tf.convert_to_tensor(s_next_batch_norm)
            state_next_batch_norm = tf.cast(state_next_batch_norm, dtype=tf.float32)
        else:
            state_batch_norm = None
            state_next_batch_norm = None
          
        # Update priorities
        if self.conf.prioritized_replay_alpha != 0:
            if self.conf.NORMALIZE_INPUTS:
                v_batch = CACTO.critic_model(state_batch_norm, training=True)                      # Compute batch of Values associated to the sampled batch ofstates
                v_next_batch = CACTO.target_critic(state_next_batch_norm, training=True)               # Compute batch of Values from target critic associated to sampled batch of next states
                vref_batch = reward_batch + (1-d_batch)*(v_next_batch)                           # Compute the targets for the TD error         
            else:
                v_batch = CACTO.critic_model(state_batch, training=True) 
                v_next_batch = CACTO.target_critic(state_next_batch, training=True)                     
                vref_batch = reward_batch + (1-d_batch)*(v_next_batch)                          

            td_errors = tf.math.abs(tf.math.subtract(vref_batch,v_batch))                           
            new_priorities = td_errors.numpy() + self.conf.prioritized_replay_eps                      # Proportional prioritization where p_i = |TD_error_i| + self.conf.prioritized_replay_eps 
            prioritized_buffer.update_priorities(batch_idxes, new_priorities)   

        # Update NNs
        self.update(episode, state_batch_norm, state_batch, state_batch_norm, state_batch, reward_batch, state_next_batch, state_next_batch_norm, d_batch, action_batch, weights_batch=weights_batch)

    def RL_Solve(self, state_prev, ep, rand_time, env, tau_TO, prioritized_buffer):
        ''' Solve RL problem '''
        DONE = 0                              # Flag indicating if the episode has terminated
        ep_return = 0                         # Initialize the return
        step_counter = 0                      # Initialize the counter of episode steps
        ep_arr = []

        # START RL EPISODE
        while True:
            # Get current and next TO actions
            action, next_TO_action = self.get_TO_actions(step_counter, action_TO=tau_TO)           

            # Simulate actions and retrieve next state and compute reward
            state_next, rwrd = env.step(rand_time, state_prev, action)
            
            # Store performed action and next state and reward
            CACTO.control_arr = np.vstack([CACTO.control_arr, action.reshape(1,self.conf.nb_action)])
            CACTO.state_arr = np.vstack([CACTO.state_arr, state_next.reshape(1,self.conf.nb_state)])
            
            CACTO.x_ee_arr.append(self.env.get_end_effector_position(CACTO.state_arr[-1,:])[0])
            CACTO.y_ee_arr.append(self.env.get_end_effector_position(CACTO.state_arr[-1,:])[1])

            ep_arr.append(rwrd)

            if step_counter==CACTO.NSTEPS_SH-1:
                DONE = 1

            # Store transition if you want to use 1-step TD
            if self.conf.TD_N==0:
                prioritized_buffer.add(state_prev, next_TO_action, action, rwrd, state_next, float(DONE))

            ep_return += rwrd                       # Increment the episodic return by the reward just recived
            step_counter += 1                       
            state_prev = np.copy(state_next)        # Next state becomes the prev state at the next episode step

            # End episode
            if DONE == 1:
                break

        # Plot the state and control trajectories of this episode 
        # plot_results(tau0_arr,tau1_arr,tau2_arr,x_ee,y_ee,x_ee_arr,y_ee_arr,NSTEPS_SH,to=TO)
        
        # Store transition after computing the (partial) cost-to go when using n-step TD (up to Monte Carlo)
        if self.conf.TD_N:
            DONE = 0
            cost_to_go_arr = []
            for i in range(len(ep_arr)):          
                final_i = min(i+self.conf.nsteps_TD_N,len(ep_arr))
                if final_i == len(ep_arr):
                    V_final = 0.0
                else:
                    if self.conf.NORMALIZE_INPUTS:
                        state_next_rollout = CACTO.state_arr[final_i,:] / self.conf.state_norm_arr
                        state_next_rollout[-1] = 2*state_next_rollout[-1] - 1
                    else:
                        state_next_rollout = CACTO.state_arr[final_i,:]
                    tf_state_next_rollout = tf.expand_dims(tf.convert_to_tensor(state_next_rollout), 0)
                    V_final = CACTO.target_critic(tf_state_next_rollout, training=False).numpy()[0][0]
                cost_to_go = sum(ep_arr[i:final_i+1]) + V_final
                cost_to_go_arr.append(np.float32(cost_to_go))
                
                if i == len(ep_arr)-1:
                    DONE = 1 
                prioritized_buffer.add(CACTO.state_arr[i,:], next_TO_action, action, cost_to_go_arr[i], np.zeros(self.conf.nb_state), float(DONE)) 
        
        # Update the NNs
        if ep>=self.conf.ep_no_update and ep%self.conf.EP_UPDATE==0:
            for i in range(self.conf.UPDATE_LOOPS):
                self.learn(ep, prioritized_buffer)  
                self.update_target(CACTO.target_critic.variables, CACTO.critic_model.variables, self.conf.UPDATE_RATE)    # Update target critic
                self.conf.update_step_counter += 1
        return self.conf.update_step_counter, ep_return

    def get_TO_actions(self, step, action_TO=None):
        ''' Get optimal actions at timestep "step" in the current episode '''
        actions = action_TO[step,:]
 
        # Buond actions in case they are not already bounded in the TO problem
        if step < CACTO.NSTEPS_SH:
            next_actions = np.clip(action_TO[step+1,:], self.conf.u_min, self.conf.u_max)
        else:
            next_actions = np.copy(actions) # Actions at last step of the episode are not performed 

        return actions, next_actions