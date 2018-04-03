#!/usr/bin/python

DOCUMENTATION = '''
---
module: ec2_asg_cutover_elb
short_description: Swap the ELBs attached to two auto scaling groups
description:
  - Swap ELBs for the specified auto scaling groups by detaching from one group and reattaching to another.
  - First, the group specified by `new_group_name` has all its ELBs detached.
  - Then, ELBs attached to the group specified by `current_group_name` are also attached to the group specified by `new_group_name`.
  - Once the instances in `new_group_name` are reporting healthy on the newly attached ELBs, the same ELBs are then detched from `current_group_name`.
  - Finally, the ELBs originally attached to `new_group_name`, or the ELBs optionally specified by `standby_load_balancers`, are attached to `current_group_name`

version_added: "2.1"
author: "Tom Bamford (@manicminer)"
options:
  current_group_name:
    description:
      - The name of the auto scaling group which is currently live in production.
    required: true
  new_group_name:
    description:
      - The name of the auto scaling group that will replace the currently live group.
    required: true
  standby_load_balancers:
    description:
      - An optional list of elastic load balancers that you wish to attach to the current auto scaling group, after it is detached from its present load balancer(s).
      - If omited, the currently live group will be attached to the load balancer(s) originally attached to the new group.
  verify_standby_instances:
    description:
      - Whether or not the task should wait for the instances in the current group to pass their ELB health checks after it is taken out of production service.
    required: false
    default: no
    choices: [ 'yes', 'no' ]
  rollback_on_failure:
    description:
      - Whether or not the task should roll back changes made to the new scaling group, if its instances do not pass ELB health checks within the period specified by `wait_timeout`.
    required: false
    default: yes
    choices: ['yes', 'no']
  wait_timeout:
    description:
      - Number of seconds to wait for the instances in the new auto scaling group to pass their ELB health checks, after switching its load balancers.
    required: false
    default: 300
extends_documentation_fragment:
    - aws
    - ec2
'''

EXAMPLES = '''
---
# Note: These examples do not set authentication details, see the AWS Guide for details.

# Perform a blue-green deployment using two auto scaling groups
# webapp-01 is the auto scaling group currently live in production
# webapp-02 is the group prepared and ready to be made live
# The two groups will have their ELBs swapped
- ec2_asg_cutover_elb:
    current_group_name: webapp-01
    new_group_name: webapp-02

# Promote the webapp-18 auto scaling group to production and move the currently live
# group webapp-17 onto a post-production elastic load balancer.
- ec2_asg_cutover_elb:
    current_group_name: webapp-17
    new_group_name: webapp-18
    standby_load_balancers:
      - webapp-post-production
'''

RETURN = '''
---
new_group:
  description: Details about the new now-in-service group
  returned: success
  type: dict
  sample:
    name: 'webapp-18'
    load_balancer_names: ['webapp-production']
    instance_ids: ['i-aaccee01', 'i-aaccee02']
    instance_status: {'i-aaccee01': ['InService', 'Healthy'], 'i-aaccee02': ['InService', 'Healthy']}
old_group:
  description: Details about the now-previous group
  returned: success
  type: dict
  sample:
    name: 'webapp-17'
    load_balancer_names: ['webapp-post-production']
    instance_ids: ['i-bbddff01', 'i-bbddff02']
    instance_status: {'i-bbddff01': ['InService', 'Healthy'], 'i-bbddff02': ['InService', 'Healthy']}
'''

try:
    import boto3
    from botocore import exceptions
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

import time

def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update(
        dict(
            current_group_name=dict(type='str', required=True),
            new_group_name=dict(type='str', required=True),
            standby_load_balancers=dict(type='list'),
            verify_standby_instances=dict(type='bool', default=False),
            rollback_on_failure=dict(type='bool', default=True),
            wait_timeout=dict(type='int', default=300),
        ),
    )
    module = AnsibleModule(argument_spec=argument_spec)

    if not HAS_BOTO3:
        module.fail_json(msg='boto3 required for this module')

    try:
        region, ec2_url, aws_connect_kwargs = get_aws_connection_info(module, boto3=True)
        autoscaling = boto3_conn(module, conn_type='client', resource='autoscaling', region=region, endpoint=ec2_url, **aws_connect_kwargs)
        elb = boto3_conn(module, conn_type='client', resource='elb', region=region, endpoint=ec2_url, **aws_connect_kwargs)
    except botocore.exceptions.ClientError, e:
        module.fail_json(msg="Boto3 Client Error - " + str(e))

    # GOOD TO KNOW
    # current_group is the auto scaling group currently in production
    # new_group is the auto scaling group you want to put into production
    # target_load_balancers is a list of ELBs attached to current_group
    # source_load_balancers is a list of ELBs attached to new_group
    # standby_load_balancers is a list of ELBs you want to attach to current_group (for future rollback)
    # original_instances is a list of instance IDs from current_group
    # current_instance_status is a dict of instance IDs from current_group with their LifecycleState/HealthStatus
    # new_instance_status is a dict of instance IDs from new_group with their LifecycleState/HealthStatus

    current_group_name = module.params.get('current_group_name')
    new_group_name = module.params.get('new_group_name')

    if current_group_name is not None and current_group_name == new_group_name:
        module.fail_json(msg="current_group_name and new_group_name cannot be the same!")

    current_groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[current_group_name])['AutoScalingGroups']
    if len(current_groups) > 1:
        module.fail_json(msg="More than one auto scaling group was found that matches the supplied current_group_name '%s'." % current_group_name)
    elif len(current_groups) < 1:
        module.fail_json(msg="The current auto scaling group '%s' was not found" % current_group_name)

    current_group = current_groups[0]

    new_groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[new_group_name])['AutoScalingGroups']
    if len(new_groups) > 1:
        module.fail_json(msg="More than one auto scaling group was found that matches the supplied new_group_name '%s'." % new_group_name)
    elif len(new_groups) < 1:
        module.fail_json(msg="The new auto scaling group '%s' was not found" % new_group_name)

    new_group = new_groups[0]

    target_load_balancers = current_group['LoadBalancerNames']
    if len(target_load_balancers) == 0:
        module.fail_json(msg="No load balancers are attached to the auto scaling group %s" % current_group['AutoScalingGroupName'])

    source_load_balancers = new_group['LoadBalancerNames']
    if len(source_load_balancers) == 0:
        module.fail_json(msg="No load balancers are attached to the auto scaling group %s" % new_group['AutoScalingGroupName'])

    standby_load_balancers = module.params.get('standby_load_balancers') or source_load_balancers
    original_instances = [i['InstanceId'] for i in current_group['Instances']]
    new_instances = [i['InstanceId'] for i in new_group['Instances']]
    current_instance_status = dict((i['InstanceId'], (i['LifecycleState'], i['HealthStatus'])) for i in current_group['Instances'])
    new_instance_status = dict((i['InstanceId'], (i['LifecycleState'], i['HealthStatus'])) for i in new_group['Instances'])

    # Before starting, ensure instances in new group are healthy and in service
    for instance_id, status in new_instance_status.iteritems():
        if status[0] != 'InService' or status[1] != 'Healthy':
            module.fail_json(msg='Instances in new_group must be healthy and in service')

    # Detach source ELB(s) from new auto scaling group
    autoscaling.detach_load_balancers(AutoScalingGroupName=new_group['AutoScalingGroupName'], LoadBalancerNames=source_load_balancers)

    # Attach target ELB(s) to new auto scaling group
    autoscaling.attach_load_balancers(AutoScalingGroupName=new_group['AutoScalingGroupName'], LoadBalancerNames=target_load_balancers)

    # Ensure ELB health check is re-enabled
    if new_group['HealthCheckType'] == 'ELB':
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=new_group['AutoScalingGroupName'], HealthCheckType=new_group['HealthCheckType'],
                                              HealthCheckGracePeriod=new_group['HealthCheckGracePeriod'])

    # Ensure instances in service with target ELB(s)
    healthy = False
    wait_timeout = time.time() + module.params.get('wait_timeout')
    while not healthy and wait_timeout > time.time():
        healthy = True

        # Iterate target load balancers and retrieve instance health
        for load_balancer in target_load_balancers:
            instance_health = elb.describe_instance_health(LoadBalancerName=load_balancer)['InstanceStates']
            instance_states = dict((i['InstanceId'], i['State']) for i in instance_health)

            # Iterate new instances and ensure they are registered/healthy
            for instance_id, status in new_instance_status.iteritems():

                # We are only concerned with new instances that were InService prior to switching the ELBs,
                # and where the new auto scaling group uses ELB health checks, that their health check passed
                if status[0] == 'InService' and (status[1] == 'Healthy' or new_group['HealthCheckType'] != 'ELB'):

                    # Ensure the instance is registered and InService according to the target ELB
                    if instance_id not in instance_states or instance_states[instance_id] != 'InService':
                        healthy = False

        if not healthy:
            time.sleep(5);
    if wait_timeout <= time.time():

        if module.params.get('rollback_on_failure'):
            # The target ELB failed to report the new instances as healthy.
            # Detach target ELB(s) and re-attach original ELB(s) to roll back to previous state.
            autoscaling.detach_load_balancers(AutoScalingGroupName=new_group['AutoScalingGroupName'], LoadBalancerNames=target_load_balancers)
            autoscaling.attach_load_balancers(AutoScalingGroupName=new_group['AutoScalingGroupName'], LoadBalancerNames=source_load_balancers)

        module.fail_json(msg='Waited too long for target ELB to report instances as healthy')

    # Detach target ELB(s) from original auto scaling group
    autoscaling.detach_load_balancers(AutoScalingGroupName=current_group['AutoScalingGroupName'], LoadBalancerNames=target_load_balancers)

    # Attach standby ELB(s) to original auto scaling group
    autoscaling.attach_load_balancers(AutoScalingGroupName=current_group['AutoScalingGroupName'], LoadBalancerNames=standby_load_balancers)

    # Ensure ELB health check is re-enabled
    if current_group['HealthCheckType'] == 'ELB':
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=current_group['AutoScalingGroupName'], HealthCheckType=current_group['HealthCheckType'],
                                              HealthCheckGracePeriod=current_group['HealthCheckGracePeriod'])

    # Ensure instances in original auto scaling group are deregistered from target ELB(s)
    deregistered = False
    wait_timeout = time.time() + module.params.get('wait_timeout')
    while not deregistered and wait_timeout > time.time():
        deregistered = True

        # Iterate target load balancers and retrieve instance health
        for load_balancer in target_load_balancers:
            instance_health = elb.describe_instance_health(LoadBalancerName=load_balancer)['InstanceStates']

            # Iterate registered instances and ensure none of the original instances are present
            for instance in instance_health:
                if instance['InstanceId'] in original_instances:
                    deregistered = False

        if not deregistered:
            time.sleep(5);
    if wait_timeout <= time.time():
        module.fail_json(msg='Waited too long for target ELB to deregister old instances')

    # Optionally verify standby ELBs' instance health
    if module.params.get('verify_standby_instances'):
        healthy = False
        wait_timeout = time.time() + module.params.get('wait_timeout')
        while not healthy and wait_timeout > time.time():
            healthy = True

            # Iterate standby load balancers and retrive instance health
            for load_balancer in standby_load_balancers:
                instance_health = elb.describe_instance_health(LoadBalancerName=load_balancer)['InstanceStates']
                instance_states = dict((i['InstanceId'], i['State']) for i in instance_health)

                # Iterate new instances and ensure they are registered/healthy
                for instance_id, status in current_instance_status.iteritems():

                    # We are only concerned with original instances that were InService prior to switching the ELBs,
                    # and where the original auto scaling group uses ELB health checks, that their health check passed
                    if status[0] == 'InService' and (status[1] == 'Healthy' or current_group['HealthCheckType'] != 'ELB'):

                        # Ensure the instance is registered and InService according to the standby ELB
                        if instance_id not in instance_states or instance_states[instance_id] != 'InService':
                            healthy = False

            if not healthy:
                time.sleep(5);
        if wait_timeout <= time.time():
            module.fail_json(msg='Waited too long for standby ELB to report instances as healthy')

    result = dict(
        new_group=dict(
            name=new_group['AutoScalingGroupName'],
            load_balancer_names=target_load_balancers,
            instance_ids=new_instances,
            instance_status=new_instance_status,
        ),
        old_group=dict(
            name=current_group['AutoScalingGroupName'],
            load_balancer_names=standby_load_balancers,
            instance_ids=original_instances,
            instance_status=current_instance_status,
        ),
    )

    module.exit_json(changed=True, result=result)


from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()
