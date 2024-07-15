"""CrewAI SWE Agent"""

import os
import typing as t

import dotenv
from composio_crewai import App, ComposioToolSet, ExecEnv
from crewai import Agent, Crew, Process, Task
from langchain_anthropic import ChatAnthropic
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from prompts import BACKSTORY, DESCRIPTION, EXPECTED_OUTPUT, GOAL, ROLE


# Load environment variables from .env
dotenv.load_dotenv()


# Initialize tool.
def get_langchain_llm() -> t.Union[ChatOpenAI, AzureChatOpenAI, ChatAnthropic]:
    helicone_api_key = os.environ.get("HELICONE_API_KEY")
    if os.environ.get("ANTHROPIC_API_KEY"):
        if helicone_api_key:
            print("Using Anthropic with Helicone")
            return ChatAnthropic(
                model_name="claude-3-5-sonnet-20240620",
                anthropic_api_url="https://anthropic.helicone.ai/",
                default_headers={
                    "Helicone-Auth": f"Bearer {helicone_api_key}",
                },
            )  # type: ignore
        print("Using Anthropic without Helicone")
        return ChatAnthropic(model_name="claude-3-5-sonnet-20240620")  # type: ignore
    if os.environ.get("OPENAI_API_KEY"):
        if helicone_api_key:
            print("Using OpenAI with Helicone")
            return ChatOpenAI(
                model="gpt-4-turbo",
                base_url="https://oai.helicone.ai/v1",
                default_headers={
                    "Helicone-Auth": f"Bearer {helicone_api_key}",
                },
            )
        print("Using OpenAI without Helicone")
        return ChatOpenAI(model="gpt-4-turbo")
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        print("Using Azure OpenAI")
        return AzureChatOpenAI(model="test")

    raise RuntimeError(
        "Could not find API key for any supported LLM models, "
        "please export either `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` "
        "or `AZURE_OPENAI_API_KEY`"
    )


composio_toolset = ComposioToolSet(workspace_env=ExecEnv.DOCKER)

# Get required tools
tools = composio_toolset.get_tools(
    apps=[
        App.SEARCHTOOL,
        App.GITCMDTOOL,
        App.FILEEDITTOOL,
    ]
)

# Define agent
agent = Agent(
    role=ROLE,
    goal=GOAL,
    backstory=BACKSTORY,
    llm=get_langchain_llm(),
    tools=tools,
    verbose=True,
)

task = Task(
    description=DESCRIPTION,
    expected_output=EXPECTED_OUTPUT,
    agent=agent,
)

crew = Crew(
    agents=[agent],
    tasks=[task],
    process=Process.sequential,
    full_output=True,
    verbose=True,
    cache=False,
    memory=True,
)