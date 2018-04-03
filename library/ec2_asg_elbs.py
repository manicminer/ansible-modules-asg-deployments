#!/usr/bin/python

DOCUMENTATION = '''
---
module: ec2_asg_elbs
short_description: Configure ELBs on an existing auto scaling group
description:
  - Configure the specified ELBs to be attached to an auto scaling group
  - The auto scaling group must already exist (use the ec2_asg module)

version_added: "2.3"
author: "Tom Bamford (@manicminer)"
options:
  name:
    description:
      - The name of the auto scaling group
    required: true
  load_balancers:
    description:
      - A list of elastic load balancers that you wish to attach to the auto scaling group.
      - Any existing attached ELBs will be detached.
  wait_timeout:
    description:
      - Number of seconds to wait for the instances to pass their ELB health checks, after switching its load balancers.
    required: false
    default: 300
extends_documentation_fragment:
    - aws
    - ec2
'''

EXAMPLES = '''
---
# Note: These examples do not set authentication details, see the AWS Guide for details.

# Set load balancers for an auto scaling group
- ec2_asg_elbs:
    name: webapp-production
    load_balancers:
      - webapp-prod-blue
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
            name=dict(type='str', required=True),
            load_balancers=dict(type='list'),
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
        module.fail_json(msg="Boto3 Client Error - " + str(e.msg))

    group_name = module.params.get('name')

    groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group_name])['AutoScalingGroups']
    if len(groups) > 1:
        module.fail_json(msg="More than one auto scaling group was found that matches the supplied group_name '%s'." % group_name)
    elif len(groups) < 1:
        module.fail_json(msg="The auto scaling group '%s' was not found" % group_name)

    group = groups[0]

    new_load_balancers = module.params.get('load_balancers')
    old_load_balancers = group['LoadBalancerNames']
    unique_new_load_balancers = [l for l in new_load_balancers if l not in old_load_balancers]
    unique_old_load_balancers = [l for l in old_load_balancers if l not in new_load_balancers]

    instances = [i['InstanceId'] for i in group['Instances']]
    instance_status = dict((i['InstanceId'], (i['LifecycleState'], i['HealthStatus'])) for i in group['Instances'])

    # Before starting, ensure instances in group are healthy and in service
    for instance_id, status in instance_status.iteritems():
        if status[0] != 'InService' or status[1] != 'Healthy':
            module.fail_json(msg='Instances in group must be healthy and in service')

    # Attach target ELB(s) to auto scaling group
    autoscaling.attach_load_balancers(AutoScalingGroupName=group['AutoScalingGroupName'], LoadBalancerNames=new_load_balancers)

    # Ensure instances in service with new ELB(s)
    healthy = False
    wait_timeout = time.time() + module.params.get('wait_timeout')
    while not healthy and wait_timeout > time.time():
        healthy = True

        # Iterate new load balancers and retrieve instance health
        for load_balancer in new_load_balancers:
            instance_health = elb.describe_instance_health(LoadBalancerName=load_balancer)['InstanceStates']
            instance_states = dict((i['InstanceId'], i['State']) for i in instance_health)

            # Iterate new instances and ensure they are registered/healthy
            for instance_id, status in instance_status.iteritems():

                # We are only concerned with new instances that were InService prior to switching the ELBs,
                # and where the auto scaling group uses ELB health checks, that their health check passed
                if status[0] == 'InService' and (status[1] == 'Healthy' or new_group['HealthCheckType'] != 'ELB'):

                    # Ensure the instance is registered and InService according to the target ELB
                    if instance_id not in instance_states or instance_states[instance_id] != 'InService':
                        healthy = False

        if not healthy:
            time.sleep(5)

    if wait_timeout <= time.time():

        if module.params.get('rollback_on_failure'):
            # The new ELB failed to report the new instances as healthy.
            # Detach unique new ELB(s) to roll back to previous state (avoid detaching any load balancers that were already attached at start)
            autoscaling.detach_load_balancers(AutoScalingGroupName=group['AutoScalingGroupName'], LoadBalancerNames=unique_new_load_balancers)

        module.fail_json(msg='Waited too long for target ELB to report instances as healthy')

    # Detach old ELB(s) from auto scaling group (unique only, we don't want to mistakenly detach any new ELBs that were specified)
    autoscaling.detach_load_balancers(AutoScalingGroupName=group['AutoScalingGroupName'], LoadBalancerNames=unique_old_load_balancers)

    result = dict(
        name=group['AutoScalingGroupName'],
        load_balancer_names=new_load_balancers,
        instance_ids=instances,
        instance_status=instance_status,
    )

    module.exit_json(changed=True, result=result)


from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()
