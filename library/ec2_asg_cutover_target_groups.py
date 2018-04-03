#!/usr/bin/python

DOCUMENTATION = '''
---
module: ec2_asg_cutover_target_groups
short_description: Swap the target groups attached to two auto scaling groups
description:
  - Swap target groups for the specified auto scaling groups by detaching from one group and reattaching to another.
  - First, the group specified by `new_group_name` has all its target groups detached.
  - Then, target groups attached to the group specified by `current_group_name` are also attached to the group specified by `new_group_name`.
  - Once the instances in `new_group_name` are reporting healthy on the newly attached target groups, the same target groups are then detached from `current_group_name`.
  - Finally, the target groups originally attached to `new_group_name`, or the target groups optionally specified by `standby_load_balancers`, are attached to `current_group_name`

version_added: "2.4"
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
  standby_target_group_arns:
    description:
      - An optional list of target group ARNs that you wish to attach to the current auto scaling group, after it is detached from its present target group(s).
      - If omitted, the currently live group will be attached to the target group(s) originally attached to the new group.
  verify_standby_instances:
    description:
      - Whether or not the task should wait for the instances in the current ASG to pass their ELB health checks after it is taken out of live service.
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
# The two groups will have their target groups swapped
- ec2_asg_cutover_target_groups:
    current_group_name: webapp-01
    new_group_name: webapp-02
'''

RETURN = '''
---
new_group:
  description: Details about the new now-in-service group
  returned: success
  type: dict
  sample:
    name: 'webapp-18'
    target_group_names: ['webapp-production']
    instance_ids: ['i-aaccee01', 'i-aaccee02']
    instance_status: {'i-aaccee01': ['InService', 'Healthy'], 'i-aaccee02': ['InService', 'Healthy']}
old_group:
  description: Details about the now-previous group
  returned: success
  type: dict
  sample:
    name: 'webapp-17'
    target_group_names: ['webapp-post-production']
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
            standby_target_group_arns=dict(type='list'),
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
        elb = boto3_conn(module, conn_type='client', resource='elbv2', region=region, endpoint=ec2_url, **aws_connect_kwargs)
    except botocore.exceptions.ClientError, e:
        module.fail_json(msg="Boto3 Client Error - " + str(e))

    # GOOD TO KNOW
    # current_group is the auto scaling group currently live
    # new_group is the auto scaling group you want to make live
    # dest_target_groups is a list of target groups attached to current_group
    # source_target_groups is a list of target groups attached to new_group
    # standby_target_groups is a list of target groups you want to attach to current_group (for future rollback)
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

    dest_target_groups = current_group['TargetGroupARNs']
    if len(dest_target_groups) == 0:
        module.fail_json(msg="No target groups are attached to the auto scaling group %s" % current_group['AutoScalingGroupName'])

    source_target_groups = new_group['TargetGroupARNs']
    if len(source_target_groups) == 0:
        module.fail_json(msg="No target groups are attached to the auto scaling group %s" % new_group['AutoScalingGroupName'])

    standby_target_groups = module.params.get('standby_target_group_arns') or source_target_groups
    original_instances = [i['InstanceId'] for i in current_group['Instances']]
    new_instances = [i['InstanceId'] for i in new_group['Instances']]
    current_instance_status = dict((i['InstanceId'], (i['LifecycleState'], i['HealthStatus'])) for i in current_group['Instances'])
    new_instance_status = dict((i['InstanceId'], (i['LifecycleState'], i['HealthStatus'])) for i in new_group['Instances'])

    # Before starting, ensure instances in new group are healthy and in service
    for instance_id, status in new_instance_status.iteritems():
        if status[0] != 'InService' or status[1] != 'Healthy':
            module.fail_json(msg='Instances in new_group must be healthy and in service')

    # Detach source target group(s) from new auto scaling group
    autoscaling.detach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=source_target_groups)

    # Attach dest target groups(s) to new auto scaling group
    autoscaling.attach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=dest_target_groups)

    # Ensure ELB health check is re-enabled
    if new_group['HealthCheckType'] == 'ELB':
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=new_group['AutoScalingGroupName'], HealthCheckType=new_group['HealthCheckType'],
                                              HealthCheckGracePeriod=new_group['HealthCheckGracePeriod'])

    # Ensure instances in service with dest target group(s)
    healthy = False
    wait_timeout = time.time() + module.params.get('wait_timeout')
    while not healthy and wait_timeout > time.time():
        healthy = True

        # Iterate new instances and ensure they are registered/healthy
        for instance_id, status in new_instance_status.iteritems():

            # We are only concerned with new instances that were InService prior to switching the target groups,
            # and where the new auto scaling group uses ELB health checks, that their health check passed
            if status[0] == 'InService' and (status[1] == 'Healthy' or new_group['HealthCheckType'] != 'ELB'):

                # Iterate dest target groups and retrieve instance health
                for target_group in dest_target_groups:
                    instance_health = elb.describe_target_health(TargetGroupArn=target_group, Targets=[{'Id': instance_id}])['TargetHealthDescriptions']

                    # Ensure the instance is registered and InService according to the dest target group
                    if len(instance_health) == 0 or instance_health[0]['TargetHealth']['State'] != 'healthy':
                        healthy = False

        if not healthy:
            time.sleep(5);

    # The dest target group failed to report the new instances as healthy.
    if wait_timeout <= time.time():

        if module.params.get('rollback_on_failure'):
            # Detach dest target group(s) and re-attach original target group(s) to roll back to previous state.
            autoscaling.detach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=dest_target_groups)
            autoscaling.attach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=source_target_groups)

            module.fail_json(msg='Waited too long for destination target group to report instances as healthy. Deployment has been rolled back.')

        else:
            module.fail_json(msg='Waited too long for destination target group to report instances as healthy. No rollback action taken.')

    # Detach dest target group(s) from original auto scaling group
    autoscaling.detach_load_balancer_target_groups(AutoScalingGroupName=current_group['AutoScalingGroupName'], TargetGroupARNs=dest_target_groups)

    # Attach standby target group(s) to original auto scaling group
    autoscaling.attach_load_balancer_target_groups(AutoScalingGroupName=current_group['AutoScalingGroupName'], TargetGroupARNs=standby_target_groups)

    # Ensure ELB health check is re-enabled
    if current_group['HealthCheckType'] == 'ELB':
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=current_group['AutoScalingGroupName'], HealthCheckType=current_group['HealthCheckType'],
                                              HealthCheckGracePeriod=current_group['HealthCheckGracePeriod'])

    # Ensure instances in original auto scaling group are deregistered from dest target group(s)
    deregistered = False
    wait_timeout = time.time() + module.params.get('wait_timeout')
    while not deregistered and wait_timeout > time.time():
        deregistered = True

        # Iterate dest target groups and retrieve instance health
        for instance in original_instances:
            for target_group in dest_target_groups:
                instance_health = elb.describe_target_health(TargetGroupArn=target_group, Targets=[{'Id': instance}])['TargetHealthDescriptions']

                # Ensure original instances are no longer registered with dest target group
                if len(instance_health) > 0:
                    if 'State' in instance_health[0]['TargetHealth'] and 'Reason' in instance_health[0]['TargetHealth']:
                        if not (instance_health[0]['TargetHealth']['State'] == 'unused' and instance_health[0]['TargetHealth']['Reason'] == 'Target.NotRegistered'):
                            deregistered = False
                    else:
                        deregistered = False

        if not deregistered:
            time.sleep(5);

    if wait_timeout <= time.time():
        if module.params.get('rollback_on_failure'):
            # Detach standby target group(s) and re-attach dest target group(s) to roll back to previous state.
            autoscaling.detach_load_balancer_target_groups(AutoScalingGroupName=current_group['AutoScalingGroupName'], TargetGroupARNs=standby_target_groups)
            autoscaling.attach_load_balancer_target_groups(AutoScalingGroupName=current_group['AutoScalingGroupName'], TargetGroupARNs=dest_target_groups)

            # Detach dest target group(s) and re-attach original target group(s) to roll back to previous state.
            autoscaling.detach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=dest_target_groups)
            autoscaling.attach_load_balancer_target_groups(AutoScalingGroupName=new_group['AutoScalingGroupName'], TargetGroupARNs=source_target_groups)

            module.fail_json(msg='Waited too long for destination target group to deregister old instances. Deployment has been rolled back.')
        else:
            module.fail_json(msg='Waited too long for destination target group to deregister old instances. No rollback action taken.')

    # Optionally verify standby target groups' instance health
    if module.params.get('verify_standby_instances'):
        healthy = False
        wait_timeout = time.time() + module.params.get('wait_timeout')
        while not healthy and wait_timeout > time.time():
            healthy = True

            # Iterate new instances and ensure they are registered/healthy
            for instance_id, status in current_instance_status.iteritems():

                # We are only concerned with original instances that were InService prior to switching the ELBs,
                # and where the original auto scaling group uses ELB health checks, that their health check passed
                if status[0] == 'InService' and (status[1] == 'Healthy' or current_group['HealthCheckType'] != 'ELB'):

                    # Iterate standby target groups and retrive instance health
                    for target_group in standby_target_groups:
                        instance_health = elb.describe_target_health(TargetGroupArn=target_group, Targets=[{'Id': instance_id}])['TargetHealthDescriptions']

                        # Ensure the instance is registered and InService according to the standby ELB
                        if len(instance_health) == 0 or instance_health[0]['TargetHealth']['State'] != 'healthy':
                            healthy = False

            if not healthy:
                time.sleep(5);
        if wait_timeout <= time.time():
            module.fail_json(msg='Waited too long for standby target group to report instances as healthy')

    result = dict(
        new_group=dict(
            name=new_group['AutoScalingGroupName'],
            target_group_arns=dest_target_groups,
            instance_ids=new_instances,
            instance_status=new_instance_status,
        ),
        old_group=dict(
            name=current_group['AutoScalingGroupName'],
            target_group_arns=standby_target_groups,
            instance_ids=original_instances,
            instance_status=current_instance_status,
        ),
    )

    module.exit_json(changed=True, result=result)


from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()
