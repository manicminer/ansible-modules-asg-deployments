import re

def format_asg_tags(input_tags):
    tags = []
    for tag in input_tags:
        new_tag = {
            tag['key']: tag['value'],
            'propagate_at_launch': tag['propagate_at_launch']
        }
        tags.append(new_tag)
    return tags


def parse_asg_tags(asg_tags):
    tags = dict()
    for asg_tag in asg_tags:
        tag_key = asg_tag['key']
        tag_val = asg_tag['value']
        tags[tag_key] = tag_val
    return tags

def asg_tag_value(asg_tags, key):
    tags = dict()
    for asg_tag in asg_tags:
        if asg_tag['key'] == key:
            return asg_tag['value']
    return None

class FilterModule(object):
    def filters(self):
        return dict(
            asg_tag_value=asg_tag_value,
            format_asg_tags=format_asg_tags,
            parse_asg_tags=parse_asg_tags,
        )

# vim: set ts=4 sts=4 sw=4 expandtab:
