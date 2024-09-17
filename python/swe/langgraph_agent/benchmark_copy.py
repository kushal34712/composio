import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.messages import HumanMessage
from typing import List
from composio_langgraph import Action

from swekit.benchmark.run_evaluation import evaluate
from swekit.config.store import IssueConfig
from langchain_aws import BedrockChat
import ast
from langgraph.errors import GraphRecursionError

from agent_testexp import get_agent_graph
import re
import random
import time
from botocore.exceptions import ClientError

max_retries = 5
base_delay = 1  # in seconds

bedrock_client = BedrockChat(
                credentials_profile_name="default",
                model_id="anthropic.claude-3-5-sonnet-20240620-v1:0",
                region_name="us-east-1",
                model_kwargs={"temperature": 0}
            )

def build_comparison_prompt(repo_name: str, issue_desc: str, patches: List[str], tests_passed: List[str]) -> str:
    patch_str = "\n".join(["="*50 + f"\nPatch {i+1}:\nTESTS STATUS: {str(tests_passed[i])}\n\n{patch}" for i, patch in enumerate(patches)]+["="*50])    
    return """
I facing the following issue in the repo {repo_name}. You have an older version of the codebase, so you your belief about the 
codebase might be outdated.

Issue Description:
{issue_desc}

You are given multiple patches and you need to check which one fixes the issue. 
Only one of the patch will fix the issue.

{patch_str}

First analyse all the patches thoroughly and then choose the best patch that fixes the issue. You need to 
consider all the edge cases very carefully. The chosen patch might be more verbose, but it should pass all the 
possible test cases regarding the issue.

NOTE: ONLY JUDGE THE PATCHES BASED ON THE CHANGES IN THE SOURCE CODE.
IGNORE THE CHANGES IN THE TESTS, DOCS OR OTHER FILES.
GIVE PREFERENCE TO THE PATCHES THAT PASS THE TESTS.

I am reiterating the issue again:
{issue_desc}

Provide your response in the following format:
{{
    "patch": "The number of the patch that best fixes the issue (1, 2, 3, ...)",
    "reasoning": "Your explanation for why the chosen patch fixes the issue",
}}
""".format(repo_name=repo_name, issue_desc=issue_desc, patch_str=patch_str)

def bench(workspace_ids: str, issue_config: IssueConfig) -> str:
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(run_agent_function, workspace_id, issue_config) for workspace_id in workspace_ids]
        results = []
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                print(f"Error in future: {e}")
    

    patches, tests_passed = zip(*results)
    valid_results = [(patch, test_passed) for patch, test_passed in results if patch and patch.strip()]
    if not valid_results:
        return ""
    
    sorted_results = sorted(valid_results, key=lambda x: "PASS" in x[1])
    patches, tests_passed = zip(*sorted_results)

    for attempt in range(max_retries):
        try:
            response = bedrock_client.invoke(
                [
                    ("system", "You are a software engineer expert at solving bugs."),
                    ("human", build_comparison_prompt(repo_name=issue_config.repo_name.split("/")[-1], issue_desc=issue_config.issue_desc, patches=patches, tests_passed=tests_passed))
                ]
            )
            break  # If successful, break out of the retry loop
        except ClientError as e:
            if attempt == max_retries - 1:  # If this was the last attempt
                raise  # Re-raise the last exception
            delay = (2 ** attempt) * base_delay  # Exponential backoff
            time.sleep(delay)
    print("Response", response)
    if response.content:
        try:
            match = re.search(r'patch.*?(\d+)', response.content, re.IGNORECASE)
            if match:
                patch_number = int(match.group(1))
                if 1 <= patch_number <= len(patches):
                    return patches[patch_number - 1]
            
            print("\n"*10)
            return random.choice(patches)
        except Exception as e:
            print("\n"*10)
            print(f"Error in response: {e}")
            return random.choice(patches)
    else:
        print("\n"*10)
        print("No response content found")
        return random.choice(patches)

def run_agent_function(workspace_id: str, issue_config: IssueConfig) -> str:
    """Run benchmark on the agent."""

    # Set the workspace for the tools to run.
    # import pdb; pdb.set_trace()
    print("Running agent function")
    
    graph, composio_toolset, test_response_file = get_agent_graph(repo_name=issue_config.repo_name.split("/")[-1] , workspace_id=workspace_id, test_command=issue_config.test_command)
    
    # get the git tree
    git_tree_response = composio_toolset.execute_action(
        action=Action.FILETOOL_GIT_REPO_TREE,
        params={},
    )
    print("Result of git tree", git_tree_response)

    cd_response = composio_toolset.execute_action(
        action=Action.SHELLTOOL_EXEC_COMMAND,
        params={"cmd": f"cd ~/{issue_config.repo_name.split('/')[-1]}"},
    )
    print("Result of cd", cd_response)

    # kick off the crew on the issue.

    try:
        run_result = graph.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=f"{issue_config.issue_desc} in the repo: {issue_config.repo_name}. Output to git tree command {git_tree_response}"
                    )
                ]
            },
            {"recursion_limit": 70},
        )
    except GraphRecursionError as e:
        print(f"GraphRecursionError: {e}")
    except Exception as e:
        print(f"Error in graph.invoke: {e}")
    
    if not os.path.exists(test_response_file):
        print(f"Test response file {test_response_file} does not exist")
        print(f"Final Patch: {patch}")
        return patch, "NOT RUN"
    
    with open(test_response_file, "r") as f:
        test_response = f.read()

    
    
    os.remove(test_response_file)

    cwd_response = composio_toolset.execute_action(
        action=Action.FILETOOL_CHANGE_WORKING_DIRECTORY,
        params={"path": f"/home/user/{issue_config.repo_name.split('/')[-1]}"},
    )
    print("Result of pwd", cwd_response)
    get_patch_resp = composio_toolset.execute_action(
        action=Action.FILETOOL_GIT_PATCH,
        params={},
    )

    print(f"Get patch response: {get_patch_resp}")
    if not get_patch_resp.get("successful", False):
        error_message = get_patch_resp.get("error")
        if error_message:
            print(f"Error in get_patch: {error_message}")
            return "", "FAIL"
        else:
            print("Unknown error occurred in get_patch")
            return "", "FAIL"

    patch_data = get_patch_resp.get("data", {})
    if not patch_data:
        print("No data found in the patch response")
        return "", "FAIL"
    patch = patch_data.get("patch")
    if not patch:
        error = patch_data.get("error")
        if error:
            print(f"Error in patch data: {error}")
            return "", "FAIL"
        else:
            print("No patch found in the response data")
            return "", "FAIL"


    response_prompt = f"""
    You are given the following test response:
    {test_response}

    You need to check if the test response is successful.
    If the test response is successful, respond with "PASS".
    If the test response is not successful, respond with "FAIL".
    """

    for attempt in range(max_retries):
        try:
            response = bedrock_client.invoke(
                [
                    ("system", "You are expert at reading test responses."),
                    ("human", response_prompt)
                ]
            )
            break  # If successful, break out of the retry loop
        except ClientError as e:
            if attempt == max_retries - 1:  # If this was the last attempt
                break  # Break out of the retry loop on the last attempt
            delay = (2 ** attempt) * base_delay  # Exponential backoff
            time.sleep(delay)
    


    print(f"Final Patch: {patch}")
    return patch, "PASS" if "PASS" in response.content else "FAIL"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run benchmark on the agent.",
    )
    # group = parser.add_mutually_exclusive_group()
    parser.add_argument(
        "--test-split",
        type=str,
        default="1:2",
        help="Test split ratio (e.g. 1:2, 1:300) Maximum 500 tests per project.",
    )
    parser.add_argument(
        "--test-instance-ids",
        type=str,
        default="",
        help="Test instance ids (comma-separated)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="temp",
        help="Run id",
    )
    args = parser.parse_args()

    if args.test_instance_ids:
        test_instance_ids_list = [
            id.strip() for id in args.test_instance_ids.split(",")
        ]
        test_range = "0:500"
    else:
        test_instance_ids_list = []
        test_range = args.test_split
    
    evaluate(
        bench,
        dry_run=False,
        test_range=test_range,
        include_hints=False,
        test_instance_ids=test_instance_ids_list,
        run_id=args.run_id,
        num_instances=3,
        #image_name="composio/composio:dev", # if you are doing local dev
    )