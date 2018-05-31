# Ansible Modules for Auto Scaling Group Deployments with ALB/ELB

This is a set of modules to orchestrate blue-green style deployments with AWS Auto Scaling Groups and Application Load Balancers / Elastic Load Balancers.

There are different modules for ALB and Classic ELB, depending on which resource is used in your environment.

Compatible with Ansible 2.3+

# Scenario 1: Blue-Green Deployments

Given 2 auto scaling groups and one ALB/ELB, the `ec2_asg_cutover_target_groups` / `ec2_asg_cutover_elb` modules can safely attach your load balancer to the green ASG, then detach it from the blue ASG, and vice versa. During this process, where the ALB/ELB is swapped between two auto scaling groups, instance health is checked at every step, and should the new instances fail to pass health checks, the deployment will be aborted and rolled back to its original state.

# Scenario 2: Pre-Live-Post Deployments

In this configuration, one auto scaling group is considered live when attached to the ALB/ELB. A new auto scaling group can be prepared and tagged as being a "pre"-stage group. When the `ec2_asg_cutover_target_groups` / `ec2_asg_cutover_elb` module runs, it attaches the ALB/ELB to the "pre" stage group, then detaches it from the "live" stage group. On completion, the "pre" stage group is retagged as the "live" stage group, and the former "live" stage group is retagged as the "post" stage group. As with the blue-green deployment scenario, if instances fail to pass health checks at any point, the deployment is rolled back to its original configuration.

A demonstration of this workflow is provided in the example playbooks.

# Reverting

Following a deployment, reverting to the previous configuration is simply a matter of performing the same deployment in reverse.
