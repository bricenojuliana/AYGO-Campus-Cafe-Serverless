"""Reads CloudFormation outputs of the deployed stack using the default AWS credential chain."""
from typing import Optional

import boto3

DEFAULT_STACK = "campus-cafe"


def stack_outputs(stack_name: str = DEFAULT_STACK, region: Optional[str] = None) -> dict[str, str]:
    client = boto3.client("cloudformation", region_name=region)
    stack = client.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
