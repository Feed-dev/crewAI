"""Test Agent creation and execution basic functionality."""

import hashlib
import json
from concurrent.futures import Future
from unittest import mock
from unittest.mock import MagicMock, patch

import instructor
import pydantic_core
import pytest
from crewai.agent import Agent
from crewai.agents.cache import CacheHandler
from crewai.crew import Crew
from crewai.crews.crew_output import CrewOutput
from crewai.memory.contextual.contextual_memory import ContextualMemory
from crewai.process import Process
from crewai.task import Task
from crewai.tasks.conditional_task import ConditionalTask
from crewai.tasks.output_format import OutputFormat
from crewai.tasks.task_output import TaskOutput
from crewai.types.usage_metrics import UsageMetrics
from crewai.utilities import Logger
from crewai.utilities.rpm_controller import RPMController
from crewai.utilities.task_output_storage_handler import TaskOutputStorageHandler

ceo = Agent(
    role="CEO",
    goal="Make sure the writers in your company produce amazing content.",
    backstory="You're an long time CEO of a content creation agency with a Senior Writer on the team. You're now working on a new project and want to make sure the content produced is amazing.",
    allow_delegation=True,
)

researcher = Agent(
    role="Researcher",
    goal="Make the best research and analysis on content about AI and AI agents",
    backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
    allow_delegation=False,
)

writer = Agent(
    role="Senior Writer",
    goal="Write the best content about AI and AI agents.",
    backstory="You're a senior writer, specialized in technology, software engineering, AI and startups. You work as a freelancer and are now working on writing content for a new customer.",
    allow_delegation=False,
)


def test_crew_config_conditional_requirement():
    with pytest.raises(ValueError):
        Crew(process=Process.sequential)

    config = json.dumps(
        {
            "agents": [
                {
                    "role": "Senior Researcher",
                    "goal": "Make the best research and analysis on content about AI and AI agents",
                    "backstory": "You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
                },
                {
                    "role": "Senior Writer",
                    "goal": "Write the best content about AI and AI agents.",
                    "backstory": "You're a senior writer, specialized in technology, software engineering, AI and startups. You work as a freelancer and are now working on writing content for a new customer.",
                },
            ],
            "tasks": [
                {
                    "description": "Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
                    "expected_output": "Bullet point list of 5 important events.",
                    "agent": "Senior Researcher",
                },
                {
                    "description": "Write a 1 amazing paragraph highlight for each idea that showcases how good an article about this topic could be, check references if necessary or search for more content but make sure it's unique, interesting and well written. Return the list of ideas with their paragraph and your notes.",
                    "expected_output": "A 4 paragraph article about AI.",
                    "agent": "Senior Writer",
                },
            ],
        }
    )
    parsed_config = json.loads(config)

    try:
        crew = Crew(process=Process.sequential, config=config)
    except ValueError:
        pytest.fail("Unexpected ValidationError raised")

    assert [agent.role for agent in crew.agents] == [
        agent["role"] for agent in parsed_config["agents"]
    ]
    assert [task.description for task in crew.tasks] == [
        task["description"] for task in parsed_config["tasks"]
    ]


def test_async_task_cannot_include_sequential_async_tasks_in_context():
    task1 = Task(
        description="Task 1",
        async_execution=True,
        expected_output="output",
        agent=researcher,
    )
    task2 = Task(
        description="Task 2",
        async_execution=True,
        expected_output="output",
        agent=researcher,
        context=[task1],
    )
    task3 = Task(
        description="Task 3",
        async_execution=True,
        expected_output="output",
        agent=researcher,
        context=[task2],
    )
    task4 = Task(
        description="Task 4",
        expected_output="output",
        agent=writer,
    )
    task5 = Task(
        description="Task 5",
        async_execution=True,
        expected_output="output",
        agent=researcher,
        context=[task4],
    )

    # This should raise an error because task2 is async and has task1 in its context without a sync task in between
    with pytest.raises(
        ValueError,
        match="Task 'Task 2' is asynchronous and cannot include other sequential asynchronous tasks in its context.",
    ):
        Crew(tasks=[task1, task2, task3, task4, task5], agents=[researcher, writer])

    # This should not raise an error because task5 has a sync task (task4) in its context
    try:
        Crew(tasks=[task1, task4, task5], agents=[researcher, writer])
    except ValueError:
        pytest.fail("Unexpected ValidationError raised")


def test_context_no_future_tasks():
    task2 = Task(
        description="Task 2",
        expected_output="output",
        agent=researcher,
    )
    task3 = Task(
        description="Task 3",
        expected_output="output",
        agent=researcher,
        context=[task2],
    )
    task4 = Task(
        description="Task 4",
        expected_output="output",
        agent=researcher,
    )
    task1 = Task(
        description="Task 1",
        expected_output="output",
        agent=researcher,
        context=[task4],
    )

    # This should raise an error because task1 has a context dependency on a future task (task4)
    with pytest.raises(
        ValueError,
        match="Task 'Task 1' has a context dependency on a future task 'Task 4', which is not allowed.",
    ):
        Crew(tasks=[task1, task2, task3, task4], agents=[researcher, writer])


def test_crew_config_with_wrong_keys():
    no_tasks_config = json.dumps(
        {
            "agents": [
                {
                    "role": "Senior Researcher",
                    "goal": "Make the best research and analysis on content about AI and AI agents",
                    "backstory": "You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
                }
            ]
        }
    )

    no_agents_config = json.dumps(
        {
            "tasks": [
                {
                    "description": "Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
                    "agent": "Senior Researcher",
                }
            ]
        }
    )
    with pytest.raises(ValueError):
        Crew(process=Process.sequential, config='{"wrong_key": "wrong_value"}')
    with pytest.raises(ValueError):
        Crew(process=Process.sequential, config=no_tasks_config)
    with pytest.raises(ValueError):
        Crew(process=Process.sequential, config=no_agents_config)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_creation():
    tasks = [
        Task(
            description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
            expected_output="Bullet point list of 5 important events.",
            agent=researcher,
        ),
        Task(
            description="Write a 1 amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
            expected_output="A 4 paragraph article about AI.",
            agent=writer,
        ),
    ]

    crew = Crew(
        agents=[researcher, writer],
        process=Process.sequential,
        tasks=tasks,
    )

    result = crew.kickoff()

    expected_string_output = "1. **AI in Healthcare Diagnostics**\n\nIn the realm of healthcare, AI is paving the way for groundbreaking advancements in diagnostic processes. By leveraging sophisticated deep learning algorithms, AI can analyze medical imaging and patient data with unprecedented accuracy, offering a transformative approach to identifying diseases early on. This innovative use of AI significantly reduces the margin of diagnostic errors and expedites the diagnosis process. For instance, conditions such as cancer or neurological disorders, which necessitate rapid and accurate detection for effective treatment, can be diagnosed much earlier and more accurately with AI, potentially transforming patient outcomes and elevating the standard of care.\n\n2. **Autonomous Agents in Financial Trading**\n\nThe financial trading industry is witnessing a revolutionary shift with the introduction of AI-powered autonomous agents. These advanced systems are designed to analyze market trends, execute trades, and manage investment portfolios in real-time without the need for human intervention. By processing vast amounts of financial data at speeds unattainable by humans, these AI agents can identify patterns, predict market movements, and make informed trading decisions that maximize returns and minimize risks. This technological leap not only enhances the efficiency of financial markets but also democratizes access to sophisticated trading strategies, leveling the playing field for individual investors.\n\n3. **AI-Powered Personal Assistants Beyond Siri and Alexa**\n\nAI personal assistants are evolving beyond simple voice commands to become indispensable parts of our daily lives, offering tailored, context-aware recommendations and handling complex tasks. Imagine a personal assistant that not only manages your schedule but also understands your preferences to make personalized online shopping suggestions or provides emotional support during stressful times. These advancements signify a deeper integration of AI into our routines, enhancing productivity and revolutionizing how we interact with technology. The potential impact on personal digital management is immense, promising a future where AI seamlessly assists in various aspects of life, from mundane chores to emotional well-being.\n\n4. **Ethical Implications of AI in Surveillance**\n\nAs AI technology advances, its application in surveillance raises critical ethical concerns. AI-driven tools like facial recognition and predictive policing can significantly enhance security measures but also pose substantial risks to privacy and civil liberties. The deployment of such technologies necessitates a careful balance between security and the protection of individual rights, as well as fostering societal trust. This tension between technological capabilities and ethical standards underscores the need for ongoing dialogue and robust regulatory frameworks to ensure that the benefits of AI-driven surveillance do not come at the expense of fundamental freedoms and societal trust.\n\n5. **AI and Creativity: From Art to Music**\n\nAI is not just a tool for analytical tasks; it is also making waves in the creative industries by producing original artworks, music compositions, and literary pieces. This fascinating blend of technology and creativity raises profound questions about the nature of creativity and the role of human artists in an era where machines can autonomously generate culturally significant works. AI's ability to mimic and innovate upon human creativity challenges traditional notions of authorship and artistic value, pushing the boundaries of what technology can achieve and offering fresh perspectives on the future of creative expression. The implications for the arts, as well as the cultural industries reliant on human creativity, are profound, suggesting a future where collaboration between humans and AI could lead to unprecedented creative heights."

    assert str(result) == expected_string_output
    assert result.raw == expected_string_output
    assert isinstance(result, CrewOutput)
    assert len(result.tasks_output) == len(tasks)
    assert result.raw == expected_string_output


@pytest.mark.vcr(filter_headers=["authorization"])
def test_sync_task_execution():
    from unittest.mock import patch

    tasks = [
        Task(
            description="Give me a list of 5 interesting ideas to explore for an article, what makes them unique and interesting.",
            expected_output="Bullet point list of 5 important events.",
            agent=researcher,
        ),
        Task(
            description="Write an amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
            expected_output="A 4 paragraph article about AI.",
            agent=writer,
        ),
    ]

    crew = Crew(
        agents=[researcher, writer],
        process=Process.sequential,
        tasks=tasks,
    )

    mock_task_output = TaskOutput(
        description="Mock description", raw="mocked output", agent="mocked agent"
    )

    # Because we are mocking execute_sync, we never hit the underlying _execute_core
    # which sets the output attribute of the task
    for task in tasks:
        task.output = mock_task_output

    with patch.object(
        Task, "execute_sync", return_value=mock_task_output
    ) as mock_execute_sync:
        crew.kickoff()

        # Assert that execute_sync was called for each task
        assert mock_execute_sync.call_count == len(tasks)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_process():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
        tasks=[task],
    )

    result = crew.kickoff()

    assert (
        result.raw
        == "1. **The Rise of Autonomous AI Agents: Transforming Industries and Everyday Life**\n   - Autonomous AI agents are revolutionizing the landscape of various industries, from healthcare to logistics, by making independent decisions that enhance efficiency and accuracy. In healthcare, these agents can diagnose diseases with remarkable precision, while in finance, they're optimizing trading strategies and risk management in real-time. Customer service has seen a transformation with AI agents offering 24/7 support, significantly improving user satisfaction. Despite their numerous advantages, the deployment of autonomous AI comes with challenges such as ethical considerations, the need for transparency, and mitigating biases in decision-making algorithms. As the technology evolves, understanding how these agents function and their potential to reshape our everyday life is becoming increasingly crucial, sparking widespread curiosity and debate.\n\n2. **Ethical Considerations in AI Development: Balancing Innovation and Responsibility**\n   - The rapid advancement of AI brings forth critical ethical considerations that need to be addressed to ensure responsible development. Issues like algorithmic bias, privacy violations, and the displacement of jobs present significant challenges. However, many companies and researchers are pioneering efforts to tackle these ethical dilemmas. For instance, some are developing frameworks for bias detection and mitigation, while others are advocating for stronger data privacy regulations. The ongoing discourse around balancing innovation with responsibility is not only relevant but essential, as it shapes the future trajectory of AI in our society. This article will provide a deep dive into these ethical challenges and highlight exemplary initiatives, making it a compelling read for developers, policymakers, and concerned citizens alike.\n\n3. **AI in Creative Fields: Revolutionizing Art, Music, and Writing**\n   - AI has made significant inroads in creative fields, sparking a new era of artistic expression and collaboration. From AI-generated paintings that fetch high prices at auctions to algorithms composing symphonies and writing novels, the intersection of creativity and technology is pushing boundaries. Renowned artists and musicians are now working alongside sophisticated AI systems, creating works that were previously unimaginable. These collaborations challenge our perceptions of creativity and originality, inviting debates about the role of human intuition versus machine learning. Highlighting these fascinating stories of innovation, this article will captivate readers who are intrigued by the limitless potential of AI to inspire and transform the arts.\n\n4. **AI-Powered Personal Assistants: The Next Generation of Smart Living**\n   - AI-powered personal assistants such as Google Assistant, Alexa, and Siri are at the forefront of the next generation of smart living, becoming more intuitive and context-aware by the day. These assistants are no longer just about setting reminders or playing music; they are evolving to understand and anticipate our needs, manage complex tasks, and even provide personalized recommendations. They are seamlessly integrating into various aspects of daily life, from home automation to health management. This article will explore the latest advancements and future directions of AI-powered personal assistants, offering readers an insight into how these innovations are shaping a smarter, more connected world.\n\n5. **The Future of Work: How AI and Automation are Reshaping the Job Market**\n   - The advent of AI and automation is fundamentally altering the job market, raising both opportunities and concerns. While certain repetitive and mundane jobs are at risk of being automated, new industries and job categories are emerging, requiring skills in AI management and oversight. Educational institutions and individuals are actively adapting by developing curricula and acquiring skills that align with these technological advancements. This article will provide a comprehensive analysis of the job roles most susceptible to automation and those that are emerging as a result of AI. It will also delve into how various sectors are preparing for this transition, making it a vital read for anyone concerned about navigating career futures in an AI-driven world."
    )


def test_manager_llm_requirement_for_hierarchical_process():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    with pytest.raises(pydantic_core._pydantic_core.ValidationError):
        Crew(
            agents=[researcher, writer],
            process=Process.hierarchical,
            tasks=[task],
        )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_manager_agent_delegating_to_assigned_task_agent():
    """
    Test that the manager agent delegates to the assigned task agent.
    """
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
        tasks=[task],
    )

    crew.kickoff()

    # Check if the manager agent has the correct tools
    assert crew.manager_agent is not None
    assert crew.manager_agent.tools is not None

    assert len(crew.manager_agent.tools) == 2
    assert (
        "Delegate a specific task to one of the following coworkers: Researcher\n"
        in crew.manager_agent.tools[0].description
    )
    assert (
        "Ask a specific question to one of the following coworkers: Researcher\n"
        in crew.manager_agent.tools[1].description
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_manager_agent_delegating_to_all_agents():
    """
    Test that the manager agent delegates to all agents when none are specified.
    """
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
        tasks=[task],
    )

    crew.kickoff()

    assert crew.manager_agent is not None
    assert crew.manager_agent.tools is not None

    assert len(crew.manager_agent.tools) == 2
    assert (
        "Delegate a specific task to one of the following coworkers: Researcher, Senior Writer\n"
        in crew.manager_agent.tools[0].description
    )
    assert (
        "Ask a specific question to one of the following coworkers: Researcher, Senior Writer\n"
        in crew.manager_agent.tools[1].description
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_with_delegating_agents():
    tasks = [
        Task(
            description="Produce and amazing 1 paragraph draft of an article about AI Agents.",
            expected_output="A 4 paragraph article about AI.",
            agent=ceo,
        )
    ]

    crew = Crew(
        agents=[ceo, writer],
        process=Process.sequential,
        tasks=tasks,
    )

    result = crew.kickoff()

    assert (
        result.raw
        == "1. **Introduction**\nAI Agents, often referred to as autonomous agents, are sophisticated software programs capable of performing tasks and making decisions independently. By leveraging advanced algorithms and vast datasets, these agents can analyze complex information and execute actions with minimal human intervention. The significance of AI Agents lies in their ability to optimize operations, provide personalized recommendations, and enhance decision-making processes across various industries.\n\n2. **Applications and Impact**\nFrom managing customer service inquiries through chatbots to optimizing supply chain logistics and even assisting in medical diagnostics, the potential applications of AI Agents are vast and transformative. In the financial sector, for instance, AI Agents can predict market trends and provide investment advice, while in healthcare, they can analyze patient data to support early diagnosis and treatment plans. The integration of AI Agents in various fields not only improves efficiency but also opens up new opportunities for innovation and growth.\n\n3. **Challenges and Ethical Considerations**\nDespite their numerous advantages, the deployment of AI Agents is not without challenges. Issues such as data privacy, algorithmic biases, and the potential for job displacement raise significant ethical and societal questions. Ensuring that AI Agents operate transparently and fairly requires robust regulatory frameworks and continuous monitoring. Additionally, as AI Agents increasingly participate in decision-making processes, it is crucial to maintain a balance between automation and human oversight to prevent unintended consequences.\n\n4. **Future Prospects**\nAs technology advances, the capabilities of AI Agents are expected to grow exponentially. Future developments may include more sophisticated natural language processing, enhanced emotional intelligence, and greater adaptability to diverse environments. These advancements could enable AI Agents to perform more complex tasks, such as creative problem-solving and strategic planning. Ultimately, the evolution of AI Agents will have a profound impact on various aspects of our lives, driving progress and innovation while reshaping the future of work and human interaction with technology."
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_verbose_output(capsys):
    tasks = [
        Task(
            description="Research AI advancements.",
            expected_output="A full report on AI advancements.",
            agent=researcher,
        ),
        Task(
            description="Write about AI in healthcare.",
            expected_output="A 4 paragraph article about AI.",
            agent=writer,
        ),
    ]

    crew = Crew(
        agents=[researcher, writer],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )

    crew.kickoff()
    captured = capsys.readouterr()
    expected_strings = [
        "\x1b[1m\x1b[95m# Agent:\x1b[00m \x1b[1m\x1b[92mResearcher",
        "\x1b[00m\n\x1b[95m## Task:\x1b[00m \x1b[92mResearch AI advancements.",
        "\x1b[1m\x1b[95m# Agent:\x1b[00m \x1b[1m\x1b[92mSenior Writer",
        "\x1b[95m## Task:\x1b[00m \x1b[92mWrite about AI in healthcare.",
        "\n\n\x1b[1m\x1b[95m# Agent:\x1b[00m \x1b[1m\x1b[92mResearcher",
        "\x1b[00m\n\x1b[95m## Final Answer:",
        "\n\n\x1b[1m\x1b[95m# Agent:\x1b[00m \x1b[1m\x1b[92mSenior Writer",
        "\x1b[00m\n\x1b[95m## Final Answer:",
    ]

    for expected_string in expected_strings:
        assert expected_string in captured.out

    # Now test with verbose set to False
    crew.verbose = False
    crew._logger = Logger(verbose=False)
    crew.kickoff()
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.vcr(filter_headers=["authorization"])
def test_cache_hitting_between_agents():
    from unittest.mock import call, patch

    from crewai_tools import tool

    @tool
    def multiplier(first_number: int, second_number: int) -> float:
        """Useful for when you need to multiply two numbers together."""
        return first_number * second_number

    tasks = [
        Task(
            description="What is 2 tims 6? Return only the number.",
            expected_output="the result of multiplication",
            tools=[multiplier],
            agent=ceo,
        ),
        Task(
            description="What is 2 times 6? Return only the number.",
            expected_output="the result of multiplication",
            tools=[multiplier],
            agent=researcher,
        ),
    ]

    crew = Crew(
        agents=[ceo, researcher],
        tasks=tasks,
    )

    with patch.object(CacheHandler, "read") as read:
        read.return_value = "12"
        crew.kickoff()
        assert read.call_count == 2, "read was not called exactly twice"
        # Check if read was called with the expected arguments
        expected_calls = [
            call(tool="multiplier", input={"first_number": 2, "second_number": 6}),
            call(tool="multiplier", input={"first_number": 2, "second_number": 6}),
        ]
        read.assert_has_calls(expected_calls, any_order=False)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_api_calls_throttling(capsys):
    from unittest.mock import patch
    from crewai_tools import tool

    @tool
    def get_final_answer() -> float:
        """Get the final answer but don't give it yet, just re-use this
        tool non-stop."""
        return 42

    agent = Agent(
        role="Very helpful assistant",
        goal="Comply with necessary changes",
        backstory="You obey orders",
        max_iter=2,
        allow_delegation=False,
        verbose=True,
        llm="gpt-4o",
    )

    task = Task(
        description="Don't give a Final Answer unless explicitly told it's time to give the absolute best final answer.",
        expected_output="The final answer.",
        tools=[get_final_answer],
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], max_rpm=1, verbose=True)

    with patch.object(RPMController, "_wait_for_next_minute") as moveon:
        moveon.return_value = True
        crew.kickoff()
        captured = capsys.readouterr()
        assert "Max RPM reached, waiting for next minute to start." in captured.out
        moveon.assert_called()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_kickoff_usage_metrics():
    inputs = [
        {"topic": "dog"},
        {"topic": "cat"},
        {"topic": "apple"},
    ]

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    results = crew.kickoff_for_each(inputs=inputs)

    assert len(results) == len(inputs)
    for result in results:
        # Assert that all required keys are in usage_metrics and their values are not None
        assert result.token_usage.total_tokens > 0
        assert result.token_usage.prompt_tokens > 0
        assert result.token_usage.completion_tokens > 0
        assert result.token_usage.successful_requests > 0


def test_agents_rpm_is_never_set_if_crew_max_RPM_is_not_set():
    agent = Agent(
        role="test role",
        goal="test goal",
        backstory="test backstory",
        allow_delegation=False,
        verbose=True,
    )

    task = Task(
        description="just say hi!",
        expected_output="your greeting",
        agent=agent,
    )

    Crew(agents=[agent], tasks=[task], verbose=True)

    assert agent._rpm_controller is None


@pytest.mark.vcr(filter_headers=["authorization"])
def test_sequential_async_task_execution_completion():
    list_ideas = Task(
        description="Give me a list of 5 interesting ideas to explore for an article, what makes them unique and interesting.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher,
        async_execution=True,
    )
    list_important_history = Task(
        description="Research the history of AI and give me the 5 most important events that shaped the technology.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher,
    )
    write_article = Task(
        description="Write an article about the history of AI and its most important events.",
        expected_output="A 4 paragraph article about AI.",
        agent=writer,
        context=[list_ideas, list_important_history],
    )

    sequential_crew = Crew(
        agents=[researcher, writer],
        process=Process.sequential,
        tasks=[list_ideas, list_important_history, write_article],
    )

    sequential_result = sequential_crew.kickoff()
    assert sequential_result.raw.startswith(
        "The history of Artificial Intelligence (AI) is a fascinating journey marked by pioneering breakthroughs and revolutionary innovations."
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_single_task_with_async_execution():
    researcher_agent = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=False,
    )

    list_ideas = Task(
        description="Generate a list of 5 interesting ideas to explore for an article, where each bulletpoint is under 15 words.",
        expected_output="Bullet point list of 5 important events. No additional commentary.",
        agent=researcher_agent,
        async_execution=True,
    )

    crew = Crew(
        agents=[researcher_agent],
        process=Process.sequential,
        tasks=[list_ideas],
    )

    result = crew.kickoff()
    assert result.raw.startswith(
        "- AI in healthcare: Revolutionizing diagnostics and personalized treatment."
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_three_task_with_async_execution():
    researcher_agent = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=False,
    )

    bullet_list = Task(
        description="Generate a list of 5 interesting ideas to explore for an article, where each bulletpoint is under 15 words.",
        expected_output="Bullet point list of 5 important events. No additional commentary.",
        agent=researcher_agent,
        async_execution=True,
    )
    numbered_list = Task(
        description="Generate a list of 5 interesting ideas to explore for an article, where each bulletpoint is under 15 words.",
        expected_output="Numbered list of 5 important events. No additional commentary.",
        agent=researcher_agent,
        async_execution=True,
    )
    letter_list = Task(
        description="Generate a list of 5 interesting ideas to explore for an article, where each bulletpoint is under 15 words.",
        expected_output="Numbered list using [A), B), C)] list of 5 important events. No additional commentary.",
        agent=researcher_agent,
        async_execution=True,
    )

    # Expected result is that we will get an error
    # because a crew can end only end with one or less
    # async tasks
    with pytest.raises(pydantic_core._pydantic_core.ValidationError) as error:
        Crew(
            agents=[researcher_agent],
            process=Process.sequential,
            tasks=[bullet_list, numbered_list, letter_list],
        )

    assert error.value.errors()[0]["type"] == "async_task_count"
    assert (
        "The crew must end with at most one asynchronous task."
        in error.value.errors()[0]["msg"]
    )


@pytest.mark.vcr(filter_headers=["authorization"])
@pytest.mark.asyncio
async def test_crew_async_kickoff():
    inputs = [
        {"topic": "dog"},
        {"topic": "cat"},
        {"topic": "apple"},
    ]

    agent = Agent(
        role="mock agent",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    mock_task_output = (
        CrewOutput(
            raw="Test output from Crew 1",
            tasks_output=[],
            token_usage=UsageMetrics(
                total_tokens=100,
                prompt_tokens=10,
                completion_tokens=90,
                successful_requests=1,
            ),
            json_dict={"output": "crew1"},
            pydantic=None,
        ),
    )
    with patch.object(Crew, "kickoff_async", return_value=mock_task_output):
        results = await crew.kickoff_for_each_async(inputs=inputs)

        assert len(results) == len(inputs)
        for result in results:
            # Assert that all required keys are in usage_metrics and their values are not None
            assert result[0].token_usage.total_tokens > 0  # type: ignore
            assert result[0].token_usage.prompt_tokens > 0  # type: ignore
            assert result[0].token_usage.completion_tokens > 0  # type: ignore
            assert result[0].token_usage.successful_requests > 0  # type: ignore


@pytest.mark.vcr(filter_headers=["authorization"])
def test_async_task_execution_call_count():
    from unittest.mock import MagicMock, patch

    list_ideas = Task(
        description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher,
        async_execution=True,
    )
    list_important_history = Task(
        description="Research the history of AI and give me the 5 most important events that shaped the technology.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher,
        async_execution=True,
    )
    write_article = Task(
        description="Write an article about the history of AI and its most important events.",
        expected_output="A 4 paragraph article about AI.",
        agent=writer,
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.sequential,
        tasks=[list_ideas, list_important_history, write_article],
    )

    # Create a valid TaskOutput instance to mock the return value
    mock_task_output = TaskOutput(
        description="Mock description", raw="mocked output", agent="mocked agent"
    )

    # Create a MagicMock Future instance
    mock_future = MagicMock(spec=Future)
    mock_future.result.return_value = mock_task_output

    # Directly set the output attribute for each task
    list_ideas.output = mock_task_output
    list_important_history.output = mock_task_output
    write_article.output = mock_task_output

    with patch.object(
        Task, "execute_sync", return_value=mock_task_output
    ) as mock_execute_sync, patch.object(
        Task, "execute_async", return_value=mock_future
    ) as mock_execute_async:
        crew.kickoff()

        assert mock_execute_async.call_count == 2
        assert mock_execute_sync.call_count == 1


@pytest.mark.vcr(filter_headers=["authorization"])
def test_kickoff_for_each_single_input():
    """Tests if kickoff_for_each works with a single input."""

    inputs = [{"topic": "dog"}]

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    results = crew.kickoff_for_each(inputs=inputs)

    assert len(results) == 1


@pytest.mark.vcr(filter_headers=["authorization"])
def test_kickoff_for_each_multiple_inputs():
    """Tests if kickoff_for_each works with multiple inputs."""

    inputs = [
        {"topic": "dog"},
        {"topic": "cat"},
        {"topic": "apple"},
    ]

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    results = crew.kickoff_for_each(inputs=inputs)

    assert len(results) == len(inputs)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_kickoff_for_each_empty_input():
    """Tests if kickoff_for_each handles an empty input list."""
    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    results = crew.kickoff_for_each(inputs=[])
    assert results == []


@pytest.mark.vcr(filter_headers=["authorization"])
def test_kickoff_for_each_invalid_input():
    """Tests if kickoff_for_each raises TypeError for invalid input types."""

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])

    with pytest.raises(TypeError):
        # Pass a string instead of a list
        crew.kickoff_for_each("invalid input")


@pytest.mark.vcr(filter_headers=["authorization"])
def test_kickoff_for_each_error_handling():
    """Tests error handling in kickoff_for_each when kickoff raises an error."""
    from unittest.mock import patch

    inputs = [
        {"topic": "dog"},
        {"topic": "cat"},
        {"topic": "apple"},
    ]
    expected_outputs = [
        "Dogs are loyal companions and popular pets.",
        "Cats are independent and low-maintenance pets.",
        "Apples are a rich source of dietary fiber and vitamin C.",
    ]
    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])

    with patch.object(Crew, "kickoff") as mock_kickoff:
        mock_kickoff.side_effect = expected_outputs[:2] + [
            Exception("Simulated kickoff error")
        ]
        with pytest.raises(Exception, match="Simulated kickoff error"):
            crew.kickoff_for_each(inputs=inputs)


@pytest.mark.vcr(filter_headers=["authorization"])
@pytest.mark.asyncio
async def test_kickoff_async_basic_functionality_and_output():
    """Tests the basic functionality and output of kickoff_async."""
    from unittest.mock import patch

    inputs = {"topic": "dog"}

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    # Create the crew
    crew = Crew(
        agents=[agent],
        tasks=[task],
    )

    expected_output = "This is a sample output from kickoff."
    with patch.object(Crew, "kickoff", return_value=expected_output) as mock_kickoff:
        result = await crew.kickoff_async(inputs)

        assert isinstance(result, str), "Result should be a string"
        assert result == expected_output, "Result should match expected output"
        mock_kickoff.assert_called_once_with(inputs)


@pytest.mark.vcr(filter_headers=["authorization"])
@pytest.mark.asyncio
async def test_async_kickoff_for_each_async_basic_functionality_and_output():
    """Tests the basic functionality and output of kickoff_for_each_async."""
    inputs = [
        {"topic": "dog"},
        {"topic": "cat"},
        {"topic": "apple"},
    ]

    # Define expected outputs for each input
    expected_outputs = [
        "Dogs are loyal companions and popular pets.",
        "Cats are independent and low-maintenance pets.",
        "Apples are a rich source of dietary fiber and vitamin C.",
    ]

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    async def mock_kickoff_async(**kwargs):
        input_data = kwargs.get("inputs")
        index = [input_["topic"] for input_ in inputs].index(input_data["topic"])
        return expected_outputs[index]

    with patch.object(
        Crew, "kickoff_async", side_effect=mock_kickoff_async
    ) as mock_kickoff_async:
        crew = Crew(agents=[agent], tasks=[task])

        results = await crew.kickoff_for_each_async(inputs)

        assert len(results) == len(inputs)
        assert results == expected_outputs
        for input_data in inputs:
            mock_kickoff_async.assert_any_call(inputs=input_data)


@pytest.mark.vcr(filter_headers=["authorization"])
@pytest.mark.asyncio
async def test_async_kickoff_for_each_async_empty_input():
    """Tests if akickoff_for_each_async handles an empty input list."""

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="1 bullet point about {topic} that's under 15 words.",
        agent=agent,
    )

    # Create the crew
    crew = Crew(
        agents=[agent],
        tasks=[task],
    )

    # Call the function we are testing
    results = await crew.kickoff_for_each_async([])

    # Assertion
    assert results == [], "Result should be an empty list when input is empty"


def test_set_agents_step_callback():
    from unittest.mock import patch

    researcher_agent = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=False,
    )

    list_ideas = Task(
        description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher_agent,
        async_execution=True,
    )

    crew = Crew(
        agents=[researcher_agent],
        process=Process.sequential,
        tasks=[list_ideas],
        step_callback=lambda: None,
    )

    with patch.object(Agent, "execute_task") as execute:
        execute.return_value = "ok"
        crew.kickoff()
        assert researcher_agent.step_callback is not None


def test_dont_set_agents_step_callback_if_already_set():
    from unittest.mock import patch

    def agent_callback(_):
        pass

    def crew_callback(_):
        pass

    researcher_agent = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=False,
        step_callback=agent_callback,
    )

    list_ideas = Task(
        description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher_agent,
        async_execution=True,
    )

    crew = Crew(
        agents=[researcher_agent],
        process=Process.sequential,
        tasks=[list_ideas],
        step_callback=crew_callback,
    )

    with patch.object(Agent, "execute_task") as execute:
        execute.return_value = "ok"
        crew.kickoff()
        assert researcher_agent.step_callback is not crew_callback
        assert researcher_agent.step_callback is agent_callback


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_function_calling_llm():
    from unittest.mock import patch
    from crewai_tools import tool

    llm = "gpt-4o"

    @tool
    def learn_about_AI() -> str:
        """Useful for when you need to learn about AI to write an paragraph about it."""
        return "AI is a very broad field."

    agent1 = Agent(
        role="test role",
        goal="test goal",
        backstory="test backstory",
        tools=[learn_about_AI],
        llm="gpt-4o-mini",
        function_calling_llm=llm,
    )

    essay = Task(
        description="Write and then review an small paragraph on AI until it's AMAZING",
        expected_output="The final paragraph.",
        agent=agent1,
    )
    tasks = [essay]
    crew = Crew(agents=[agent1], tasks=tasks)

    with patch.object(
        instructor, "from_litellm", wraps=instructor.from_litellm
    ) as mock_from_litellm:
        crew.kickoff()
        mock_from_litellm.assert_called()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_task_with_no_arguments():
    from crewai_tools import tool

    @tool
    def return_data() -> str:
        "Useful to get the sales related data"
        return "January: 5, February: 10, March: 15, April: 20, May: 25"

    researcher = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        tools=[return_data],
        allow_delegation=False,
    )

    task = Task(
        description="Look at the available data and give me a sense on the total number of sales.",
        expected_output="The total number of sales as an integer",
        agent=researcher,
    )

    crew = Crew(agents=[researcher], tasks=[task])

    result = crew.kickoff()
    assert result.raw == "75"


def test_code_execution_flag_adds_code_tool_upon_kickoff():
    from crewai_tools import CodeInterpreterTool

    programmer = Agent(
        role="Programmer",
        goal="Write code to solve problems.",
        backstory="You're a programmer who loves to solve problems with code.",
        allow_delegation=False,
        allow_code_execution=True,
    )

    task = Task(
        description="How much is 2 + 2?",
        expected_output="The result of the sum as an integer.",
        agent=programmer,
    )

    crew = Crew(agents=[programmer], tasks=[task])

    with patch.object(Agent, "execute_task") as executor:
        executor.return_value = "ok"
        crew.kickoff()
        assert len(programmer.tools) == 1
        assert programmer.tools[0].__class__ == CodeInterpreterTool


@pytest.mark.vcr(filter_headers=["authorization"])
def test_delegation_is_not_enabled_if_there_are_only_one_agent():
    researcher = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=True,
    )

    task = Task(
        description="Look at the available data and give me a sense on the total number of sales.",
        expected_output="The total number of sales as an integer",
        agent=researcher,
    )

    crew = Crew(agents=[researcher], tasks=[task])

    crew.kickoff()
    assert task.tools == []


@pytest.mark.vcr(filter_headers=["authorization"])
def test_agents_do_not_get_delegation_tools_with_there_is_only_one_agent():
    agent = Agent(
        role="Researcher",
        goal="Be super empathetic.",
        backstory="You're love to sey howdy.",
        allow_delegation=False,
    )

    task = Task(description="say howdy", expected_output="Howdy!", agent=agent)

    crew = Crew(agents=[agent], tasks=[task])

    result = crew.kickoff()
    assert result.raw == "Howdy!"
    assert len(agent.tools) == 0


@pytest.mark.vcr(filter_headers=["authorization"])
def test_sequential_crew_creation_tasks_without_agents():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        # agent=researcher, # not having an agent on the task should throw an error
    )

    # Expected Output: The sequential crew should fail to create because the task is missing an agent
    with pytest.raises(pydantic_core._pydantic_core.ValidationError) as exec_info:
        Crew(
            tasks=[task],
            agents=[researcher],
            process=Process.sequential,
        )

    assert exec_info.value.errors()[0]["type"] == "missing_agent_in_task"
    assert (
        "Agent is missing in the task with the following description"
        in exec_info.value.errors()[0]["msg"]
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_agent_usage_metrics_are_captured_for_hierarchical_process():
    agent = Agent(
        role="Researcher",
        goal="Be super empathetic.",
        backstory="You're love to sey howdy.",
        allow_delegation=False,
    )

    task = Task(description="Ask the researched to say hi!", expected_output="Howdy!")

    crew = Crew(
        agents=[agent], tasks=[task], process=Process.hierarchical, manager_llm="gpt-4o"
    )

    result = crew.kickoff()
    assert result.raw == "Howdy!"

    assert result.token_usage == UsageMetrics(
        total_tokens=2701,
        prompt_tokens=2550,
        completion_tokens=151,
        successful_requests=5,
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_crew_creation_tasks_with_agents():
    """
    Agents are not required for tasks in a hierarchical process but sometimes they are still added
    This test makes sure that the manager still delegates the task to the agent even if the agent is passed in the task
    """
    task = Task(
        description="Write one amazing paragraph about AI.",
        expected_output="A single paragraph with 4 sentences.",
        agent=writer,
    )

    crew = Crew(
        tasks=[task],
        agents=[writer, researcher],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
    )
    crew.kickoff()

    assert crew.manager_agent is not None
    assert crew.manager_agent.tools is not None
    assert crew.manager_agent.tools[0].description.startswith(
        "Delegate a specific task to one of the following coworkers: Senior Writer"
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_crew_creation_tasks_with_async_execution():
    """
    Agents are not required for tasks in a hierarchical process but sometimes they are still added
    This test makes sure that the manager still delegates the task to the agent even if the agent is passed in the task
    """
    task = Task(
        description="Write one amazing paragraph about AI.",
        expected_output="A single paragraph with 4 sentences.",
        agent=writer,
        async_execution=True,
    )

    crew = Crew(
        tasks=[task],
        agents=[writer, researcher, ceo],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
    )

    crew.kickoff()
    assert crew.manager_agent is not None
    assert crew.manager_agent.tools is not None
    assert crew.manager_agent.tools[0].description.startswith(
        "Delegate a specific task to one of the following coworkers: Senior Writer\n"
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_crew_creation_tasks_with_sync_last():
    """
    Agents are not required for tasks in a hierarchical process but sometimes they are still added
    This test makes sure that the manager still delegates the task to the agent even if the agent is passed in the task
    """
    task = Task(
        description="Write one amazing paragraph about AI.",
        expected_output="A single paragraph with 4 sentences.",
        agent=writer,
        async_execution=True,
    )
    task2 = Task(
        description="Write one amazing paragraph about AI.",
        expected_output="A single paragraph with 4 sentences.",
        async_execution=False,
    )

    crew = Crew(
        tasks=[task, task2],
        agents=[writer, researcher, ceo],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
    )

    crew.kickoff()
    assert crew.manager_agent is not None
    assert crew.manager_agent.tools is not None
    assert crew.manager_agent.tools[0].description.startswith(
        "Delegate a specific task to one of the following coworkers: Senior Writer, Researcher, CEO\n"
    )


def test_crew_inputs_interpolate_both_agents_and_tasks():
    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="{points} bullet points about {topic}.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])
    inputs = {"topic": "AI", "points": 5}
    crew._interpolate_inputs(inputs=inputs)  # Manual call for now

    assert crew.tasks[0].description == "Give me an analysis around AI."
    assert crew.tasks[0].expected_output == "5 bullet points about AI."
    assert crew.agents[0].role == "AI Researcher"
    assert crew.agents[0].goal == "Express hot takes on AI."
    assert crew.agents[0].backstory == "You have a lot of experience with AI."


def test_crew_inputs_interpolate_both_agents_and_tasks_diff():
    from unittest.mock import patch

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="{points} bullet points about {topic}.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])

    with patch.object(Agent, "execute_task") as execute:
        with patch.object(
            Agent, "interpolate_inputs", wraps=agent.interpolate_inputs
        ) as interpolate_agent_inputs:
            with patch.object(
                Task, "interpolate_inputs", wraps=task.interpolate_inputs
            ) as interpolate_task_inputs:
                execute.return_value = "ok"
                crew.kickoff(inputs={"topic": "AI", "points": 5})
                interpolate_agent_inputs.assert_called()
                interpolate_task_inputs.assert_called()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_does_not_interpolate_without_inputs():
    from unittest.mock import patch

    agent = Agent(
        role="{topic} Researcher",
        goal="Express hot takes on {topic}.",
        backstory="You have a lot of experience with {topic}.",
    )

    task = Task(
        description="Give me an analysis around {topic}.",
        expected_output="{points} bullet points about {topic}.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])

    with patch.object(Agent, "interpolate_inputs") as interpolate_agent_inputs:
        with patch.object(Task, "interpolate_inputs") as interpolate_task_inputs:
            crew.kickoff()
            interpolate_agent_inputs.assert_not_called()
            interpolate_task_inputs.assert_not_called()


# def test_crew_partial_inputs():
#     agent = Agent(
#         role="{topic} Researcher",
#         goal="Express hot takes on {topic}.",
#         backstory="You have a lot of experience with {topic}.",
#     )

#     task = Task(
#         description="Give me an analysis around {topic}.",
#         expected_output="{points} bullet points about {topic}.",
#     )

#     crew = Crew(agents=[agent], tasks=[task], inputs={"topic": "AI"})
#     inputs = {"topic": "AI"}
#     crew._interpolate_inputs(inputs=inputs)  # Manual call for now

#     assert crew.tasks[0].description == "Give me an analysis around AI."
#     assert crew.tasks[0].expected_output == "{points} bullet points about AI."
#     assert crew.agents[0].role == "AI Researcher"
#     assert crew.agents[0].goal == "Express hot takes on AI."
#     assert crew.agents[0].backstory == "You have a lot of experience with AI."


# def test_crew_invalid_inputs():
#     agent = Agent(
#         role="{topic} Researcher",
#         goal="Express hot takes on {topic}.",
#         backstory="You have a lot of experience with {topic}.",
#     )

#     task = Task(
#         description="Give me an analysis around {topic}.",
#         expected_output="{points} bullet points about {topic}.",
#     )

#     crew = Crew(agents=[agent], tasks=[task], inputs={"subject": "AI"})
#     inputs = {"subject": "AI"}
#     crew._interpolate_inputs(inputs=inputs)  # Manual call for now

#     assert crew.tasks[0].description == "Give me an analysis around {topic}."
#     assert crew.tasks[0].expected_output == "{points} bullet points about {topic}."
#     assert crew.agents[0].role == "{topic} Researcher"
#     assert crew.agents[0].goal == "Express hot takes on {topic}."
#     assert crew.agents[0].backstory == "You have a lot of experience with {topic}."


def test_task_callback_on_crew():
    from unittest.mock import MagicMock, patch

    researcher_agent = Agent(
        role="Researcher",
        goal="Make the best research and analysis on content about AI and AI agents",
        backstory="You're an expert researcher, specialized in technology, software engineering, AI and startups. You work as a freelancer and is now working on doing research and analysis for a new customer.",
        allow_delegation=False,
    )

    list_ideas = Task(
        description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
        expected_output="Bullet point list of 5 important events.",
        agent=researcher_agent,
        async_execution=True,
    )

    mock_callback = MagicMock()

    crew = Crew(
        agents=[researcher_agent],
        process=Process.sequential,
        tasks=[list_ideas],
        task_callback=mock_callback,
    )

    with patch.object(Agent, "execute_task") as execute:
        execute.return_value = "ok"
        crew.kickoff()

        assert list_ideas.callback is not None
        mock_callback.assert_called_once()
        args, _ = mock_callback.call_args
        assert isinstance(args[0], TaskOutput)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_tools_with_custom_caching():
    from unittest.mock import patch

    from crewai_tools import tool

    @tool
    def multiplcation_tool(first_number: int, second_number: int) -> int:
        """Useful for when you need to multiply two numbers together."""
        return first_number * second_number

    def cache_func(args, result):
        cache = result % 2 == 0
        return cache

    multiplcation_tool.cache_function = cache_func

    writer1 = Agent(
        role="Writer",
        goal="You write lessons of math for kids.",
        backstory="You're an expert in writing and you love to teach kids but you know nothing of math.",
        tools=[multiplcation_tool],
        allow_delegation=False,
    )

    writer2 = Agent(
        role="Writer",
        goal="You write lessons of math for kids.",
        backstory="You're an expert in writing and you love to teach kids but you know nothing of math.",
        tools=[multiplcation_tool],
        allow_delegation=False,
    )

    task1 = Task(
        description="What is 2 times 6? Return only the number after using the multiplication tool.",
        expected_output="the result of multiplication",
        agent=writer1,
    )

    task2 = Task(
        description="What is 3 times 1? Return only the number after using the multiplication tool.",
        expected_output="the result of multiplication",
        agent=writer1,
    )

    task3 = Task(
        description="What is 2 times 6? Return only the number after using the multiplication tool.",
        expected_output="the result of multiplication",
        agent=writer2,
    )

    task4 = Task(
        description="What is 3 times 1? Return only the number after using the multiplication tool.",
        expected_output="the result of multiplication",
        agent=writer2,
    )

    crew = Crew(agents=[writer1, writer2], tasks=[task1, task2, task3, task4])

    with patch.object(
        CacheHandler, "add", wraps=crew._cache_handler.add
    ) as add_to_cache:
        with patch.object(CacheHandler, "read", wraps=crew._cache_handler.read) as _:
            result = crew.kickoff()
            add_to_cache.assert_called_once_with(
                tool="multiplcation_tool",
                input={"first_number": 2, "second_number": 6},
                output=12,
            )
            assert result.raw == "3"


@pytest.mark.vcr(filter_headers=["authorization"])
def test_using_contextual_memory():
    from unittest.mock import patch

    math_researcher = Agent(
        role="Researcher",
        goal="You research about math.",
        backstory="You're an expert in research and you love to learn new things.",
        allow_delegation=False,
    )

    task1 = Task(
        description="Research a topic to teach a kid aged 6 about math.",
        expected_output="A topic, explanation, angle, and examples.",
        agent=math_researcher,
    )

    crew = Crew(
        agents=[math_researcher],
        tasks=[task1],
        memory=True,
    )

    with patch.object(ContextualMemory, "build_context_for_task") as contextual_mem:
        crew.kickoff()
        contextual_mem.assert_called_once()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_disabled_memory_using_contextual_memory():
    from unittest.mock import patch

    math_researcher = Agent(
        role="Researcher",
        goal="You research about math.",
        backstory="You're an expert in research and you love to learn new things.",
        allow_delegation=False,
    )

    task1 = Task(
        description="Research a topic to teach a kid aged 6 about math.",
        expected_output="A topic, explanation, angle, and examples.",
        agent=math_researcher,
    )

    crew = Crew(
        agents=[math_researcher],
        tasks=[task1],
        memory=False,
    )

    with patch.object(ContextualMemory, "build_context_for_task") as contextual_mem:
        crew.kickoff()
        contextual_mem.assert_not_called()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_log_file_output(tmp_path):
    test_file = tmp_path / "logs.txt"
    tasks = [
        Task(
            description="Say Hi",
            expected_output="The word: Hi",
            agent=researcher,
        )
    ]

    crew = Crew(agents=[researcher], tasks=tasks, output_log_file=str(test_file))
    crew.kickoff()
    assert test_file.exists()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_manager_agent():
    from unittest.mock import patch

    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    manager = Agent(
        role="Manager",
        goal="Manage the crew and ensure the tasks are completed efficiently.",
        backstory="You're an experienced manager, skilled in overseeing complex projects and guiding teams to success. Your role is to coordinate the efforts of the crew members, ensuring that each task is completed on time and to the highest standard.",
        allow_delegation=False,
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.hierarchical,
        manager_agent=manager,
        tasks=[task],
    )

    mock_task_output = TaskOutput(
        description="Mock description", raw="mocked output", agent="mocked agent"
    )

    # Because we are mocking execute_sync, we never hit the underlying _execute_core
    # which sets the output attribute of the task
    task.output = mock_task_output

    with patch.object(
        Task, "execute_sync", return_value=mock_task_output
    ) as mock_execute_sync:
        crew.kickoff()
        assert manager.allow_delegation is True
        mock_execute_sync.assert_called()


def test_manager_agent_in_agents_raises_exception():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    manager = Agent(
        role="Manager",
        goal="Manage the crew and ensure the tasks are completed efficiently.",
        backstory="You're an experienced manager, skilled in overseeing complex projects and guiding teams to success. Your role is to coordinate the efforts of the crew members, ensuring that each task is completed on time and to the highest standard.",
        allow_delegation=False,
    )

    with pytest.raises(pydantic_core._pydantic_core.ValidationError):
        Crew(
            agents=[researcher, writer, manager],
            process=Process.hierarchical,
            manager_agent=manager,
            tasks=[task],
        )


def test_manager_agent_with_tools_raises_exception():
    from crewai_tools import tool

    @tool
    def testing_tool(first_number: int, second_number: int) -> int:
        """Useful for when you need to multiply two numbers together."""
        return first_number * second_number

    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    manager = Agent(
        role="Manager",
        goal="Manage the crew and ensure the tasks are completed efficiently.",
        backstory="You're an experienced manager, skilled in overseeing complex projects and guiding teams to success. Your role is to coordinate the efforts of the crew members, ensuring that each task is completed on time and to the highest standard.",
        allow_delegation=False,
        tools=[testing_tool],
    )

    crew = Crew(
        agents=[researcher, writer],
        process=Process.hierarchical,
        manager_agent=manager,
        tasks=[task],
    )

    with pytest.raises(Exception):
        crew.kickoff()


@patch("crewai.crew.Crew.kickoff")
@patch("crewai.crew.CrewTrainingHandler")
@patch("crewai.crew.TaskEvaluator")
def test_crew_train_success(task_evaluator, crew_training_handler, kickoff):
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task],
    )
    crew.train(
        n_iterations=2, inputs={"topic": "AI"}, filename="trained_agents_data.pkl"
    )
    task_evaluator.assert_has_calls(
        [
            mock.call(researcher),
            mock.call().evaluate_training_data(
                training_data=crew_training_handler().load(),
                agent_id=str(researcher.id),
            ),
            mock.call().evaluate_training_data().model_dump(),
            mock.call(writer),
            mock.call().evaluate_training_data(
                training_data=crew_training_handler().load(),
                agent_id=str(writer.id),
            ),
            mock.call().evaluate_training_data().model_dump(),
        ]
    )

    crew_training_handler.assert_has_calls(
        [
            mock.call("training_data.pkl"),
            mock.call().load(),
            mock.call("trained_agents_data.pkl"),
            mock.call().save_trained_data(
                agent_id="Researcher",
                trained_data=task_evaluator().evaluate_training_data().model_dump(),
            ),
            mock.call("trained_agents_data.pkl"),
            mock.call().save_trained_data(
                agent_id="Senior Writer",
                trained_data=task_evaluator().evaluate_training_data().model_dump(),
            ),
            mock.call(),
            mock.call().load(),
            mock.call(),
            mock.call().load(),
        ]
    )

    kickoff.assert_has_calls(
        [mock.call(inputs={"topic": "AI"}), mock.call(inputs={"topic": "AI"})]
    )


def test_crew_train_error():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task],
    )

    with pytest.raises(TypeError) as e:
        crew.train()
        assert "train() missing 1 required positional argument: 'n_iterations'" in str(
            e
        )


def test__setup_for_training():
    researcher.allow_delegation = True
    writer.allow_delegation = True
    agents = [researcher, writer]
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=agents,
        tasks=[task],
    )

    assert crew._train is False
    assert task.human_input is False

    for agent in agents:
        assert agent.allow_delegation is True

    crew._setup_for_training("trained_agents_data.pkl")

    assert crew._train is True
    assert task.human_input is True

    for agent in agents:
        assert agent.allow_delegation is False


@pytest.mark.vcr(filter_headers=["authorization"])
def test_replay_feature():
    list_ideas = Task(
        description="Generate a list of 5 interesting ideas to explore for an article, where each bulletpoint is under 15 words.",
        expected_output="Bullet point list of 5 important events. No additional commentary.",
        agent=researcher,
    )
    write = Task(
        description="Write a sentence about the events",
        expected_output="A sentence about the events",
        agent=writer,
        context=[list_ideas],
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[list_ideas, write],
        process=Process.sequential,
    )

    with patch.object(Task, "execute_sync") as mock_execute_task:
        mock_execute_task.return_value = TaskOutput(
            description="Mock description",
            raw="Mocked output for list of ideas",
            agent="Researcher",
            json_dict=None,
            output_format=OutputFormat.RAW,
            pydantic=None,
            summary="Mocked output for list of ideas",
        )

        crew.kickoff()
        crew.replay(str(write.id))
        # Ensure context was passed correctly
        assert mock_execute_task.call_count == 3


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_replay_error():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task],
    )

    with pytest.raises(TypeError) as e:
        crew.replay()  # type: ignore purposefully throwing err
        assert "task_id is required" in str(e)


@pytest.mark.vcr(filter_headers=["authorization"])
def test_crew_task_db_init():
    agent = Agent(
        role="Content Writer",
        goal="Write engaging content on various topics.",
        backstory="You have a background in journalism and creative writing.",
    )

    task = Task(
        description="Write a detailed article about AI in healthcare.",
        expected_output="A 1 paragraph article about AI.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task])

    with patch.object(Task, "execute_sync") as mock_execute_task:
        mock_execute_task.return_value = TaskOutput(
            description="Write about AI in healthcare.",
            raw="Artificial Intelligence (AI) is revolutionizing healthcare by enhancing diagnostic accuracy, personalizing treatment plans, and streamlining administrative tasks.",
            agent="Content Writer",
            json_dict=None,
            output_format=OutputFormat.RAW,
            pydantic=None,
            summary="Write about AI in healthcare...",
        )

        crew.kickoff()

        # Check if this runs without raising an exception
        try:
            db_handler = TaskOutputStorageHandler()
            db_handler.load()
            assert True  # If we reach this point, no exception was raised
        except Exception as e:
            pytest.fail(f"An exception was raised: {str(e)}")


@pytest.mark.vcr(filter_headers=["authorization"])
def test_replay_task_with_context():
    agent1 = Agent(
        role="Researcher",
        goal="Research AI advancements.",
        backstory="You are an expert in AI research.",
    )
    agent2 = Agent(
        role="Writer",
        goal="Write detailed articles on AI.",
        backstory="You have a background in journalism and AI.",
    )

    task1 = Task(
        description="Research the latest advancements in AI.",
        expected_output="A detailed report on AI advancements.",
        agent=agent1,
    )
    task2 = Task(
        description="Summarize the AI advancements report.",
        expected_output="A summary of the AI advancements report.",
        agent=agent2,
    )
    task3 = Task(
        description="Write an article based on the AI advancements summary.",
        expected_output="An article on AI advancements.",
        agent=agent2,
    )
    task4 = Task(
        description="Create a presentation based on the AI advancements article.",
        expected_output="A presentation on AI advancements.",
        agent=agent2,
        context=[task1],
    )

    crew = Crew(
        agents=[agent1, agent2],
        tasks=[task1, task2, task3, task4],
        process=Process.sequential,
    )

    mock_task_output1 = TaskOutput(
        description="Research the latest advancements in AI.",
        raw="Detailed report on AI advancements...",
        agent="Researcher",
        json_dict=None,
        output_format=OutputFormat.RAW,
        pydantic=None,
        summary="Detailed report on AI advancements...",
    )
    mock_task_output2 = TaskOutput(
        description="Summarize the AI advancements report.",
        raw="Summary of the AI advancements report...",
        agent="Writer",
        json_dict=None,
        output_format=OutputFormat.RAW,
        pydantic=None,
        summary="Summary of the AI advancements report...",
    )
    mock_task_output3 = TaskOutput(
        description="Write an article based on the AI advancements summary.",
        raw="Article on AI advancements...",
        agent="Writer",
        json_dict=None,
        output_format=OutputFormat.RAW,
        pydantic=None,
        summary="Article on AI advancements...",
    )
    mock_task_output4 = TaskOutput(
        description="Create a presentation based on the AI advancements article.",
        raw="Presentation on AI advancements...",
        agent="Writer",
        json_dict=None,
        output_format=OutputFormat.RAW,
        pydantic=None,
        summary="Presentation on AI advancements...",
    )

    with patch.object(Task, "execute_sync") as mock_execute_task:
        mock_execute_task.side_effect = [
            mock_task_output1,
            mock_task_output2,
            mock_task_output3,
            mock_task_output4,
        ]

        crew.kickoff()
        db_handler = TaskOutputStorageHandler()
        assert db_handler.load() != []

        with patch.object(Task, "execute_sync") as mock_replay_task:
            mock_replay_task.return_value = mock_task_output4

            replayed_output = crew.replay(str(task4.id))
            assert replayed_output.raw == "Presentation on AI advancements..."

        db_handler.reset()


@pytest.mark.vcr(filter_headers=["authorization"])
def test_replay_with_context():
    agent = Agent(role="test_agent", backstory="Test Description", goal="Test Goal")
    task1 = Task(
        description="Context Task", expected_output="Say Task Output", agent=agent
    )
    task2 = Task(
        description="Test Task", expected_output="Say Hi", agent=agent, context=[task1]
    )

    context_output = TaskOutput(
        description="Context Task Output",
        agent="test_agent",
        raw="context raw output",
        pydantic=None,
        json_dict={},
        output_format=OutputFormat.RAW,
    )
    task1.output = context_output

    crew = Crew(agents=[agent], tasks=[task1, task2], process=Process.sequential)

    with patch(
        "crewai.utilities.task_output_storage_handler.TaskOutputStorageHandler.load",
        return_value=[
            {
                "task_id": str(task1.id),
                "output": {
                    "description": context_output.description,
                    "summary": context_output.summary,
                    "raw": context_output.raw,
                    "pydantic": context_output.pydantic,
                    "json_dict": context_output.json_dict,
                    "output_format": context_output.output_format,
                    "agent": context_output.agent,
                },
                "inputs": {},
            },
            {
                "task_id": str(task2.id),
                "output": {
                    "description": "Test Task Output",
                    "summary": None,
                    "raw": "test raw output",
                    "pydantic": None,
                    "json_dict": {},
                    "output_format": "json",
                    "agent": "test_agent",
                },
                "inputs": {},
            },
        ],
    ):
        crew.replay(str(task2.id))

        assert crew.tasks[1].context[0].output.raw == "context raw output"


@pytest.mark.vcr(filter_headers=["authorization"])
def test_replay_with_invalid_task_id():
    agent = Agent(role="test_agent", backstory="Test Description", goal="Test Goal")
    task1 = Task(
        description="Context Task", expected_output="Say Task Output", agent=agent
    )
    task2 = Task(
        description="Test Task", expected_output="Say Hi", agent=agent, context=[task1]
    )

    context_output = TaskOutput(
        description="Context Task Output",
        agent="test_agent",
        raw="context raw output",
        pydantic=None,
        json_dict={},
        output_format=OutputFormat.RAW,
    )
    task1.output = context_output

    crew = Crew(agents=[agent], tasks=[task1, task2], process=Process.sequential)

    with patch(
        "crewai.utilities.task_output_storage_handler.TaskOutputStorageHandler.load",
        return_value=[
            {
                "task_id": str(task1.id),
                "output": {
                    "description": context_output.description,
                    "summary": context_output.summary,
                    "raw": context_output.raw,
                    "pydantic": context_output.pydantic,
                    "json_dict": context_output.json_dict,
                    "output_format": context_output.output_format,
                    "agent": context_output.agent,
                },
                "inputs": {},
            },
            {
                "task_id": str(task2.id),
                "output": {
                    "description": "Test Task Output",
                    "summary": None,
                    "raw": "test raw output",
                    "pydantic": None,
                    "json_dict": {},
                    "output_format": "json",
                    "agent": "test_agent",
                },
                "inputs": {},
            },
        ],
    ):
        with pytest.raises(
            ValueError,
            match="Task with id bf5b09c9-69bd-4eb8-be12-f9e5bae31c2d not found in the crew's tasks.",
        ):
            crew.replay("bf5b09c9-69bd-4eb8-be12-f9e5bae31c2d")


@pytest.mark.vcr(filter_headers=["authorization"])
@patch.object(Crew, "_interpolate_inputs")
def test_replay_interpolates_inputs_properly(mock_interpolate_inputs):
    agent = Agent(role="test_agent", backstory="Test Description", goal="Test Goal")
    task1 = Task(description="Context Task", expected_output="Say {name}", agent=agent)
    task2 = Task(
        description="Test Task",
        expected_output="Say Hi to {name}",
        agent=agent,
        context=[task1],
    )

    context_output = TaskOutput(
        description="Context Task Output",
        agent="test_agent",
        raw="context raw output",
        pydantic=None,
        json_dict={},
        output_format=OutputFormat.RAW,
    )
    task1.output = context_output

    crew = Crew(agents=[agent], tasks=[task1, task2], process=Process.sequential)
    crew.kickoff(inputs={"name": "John"})

    with patch(
        "crewai.utilities.task_output_storage_handler.TaskOutputStorageHandler.load",
        return_value=[
            {
                "task_id": str(task1.id),
                "output": {
                    "description": context_output.description,
                    "summary": context_output.summary,
                    "raw": context_output.raw,
                    "pydantic": context_output.pydantic,
                    "json_dict": context_output.json_dict,
                    "output_format": context_output.output_format,
                    "agent": context_output.agent,
                },
                "inputs": {"name": "John"},
            },
            {
                "task_id": str(task2.id),
                "output": {
                    "description": "Test Task Output",
                    "summary": None,
                    "raw": "test raw output",
                    "pydantic": None,
                    "json_dict": {},
                    "output_format": "json",
                    "agent": "test_agent",
                },
                "inputs": {"name": "John"},
            },
        ],
    ):
        crew.replay(str(task2.id))
        assert crew._inputs == {"name": "John"}
        assert mock_interpolate_inputs.call_count == 2


@pytest.mark.vcr(filter_headers=["authorization"])
def test_replay_setup_context():
    agent = Agent(role="test_agent", backstory="Test Description", goal="Test Goal")
    task1 = Task(description="Context Task", expected_output="Say {name}", agent=agent)
    task2 = Task(
        description="Test Task",
        expected_output="Say Hi to {name}",
        agent=agent,
    )
    context_output = TaskOutput(
        description="Context Task Output",
        agent="test_agent",
        raw="context raw output",
        pydantic=None,
        json_dict={},
        output_format=OutputFormat.RAW,
    )
    task1.output = context_output
    crew = Crew(agents=[agent], tasks=[task1, task2], process=Process.sequential)
    with patch(
        "crewai.utilities.task_output_storage_handler.TaskOutputStorageHandler.load",
        return_value=[
            {
                "task_id": str(task1.id),
                "output": {
                    "description": context_output.description,
                    "summary": context_output.summary,
                    "raw": context_output.raw,
                    "pydantic": context_output.pydantic,
                    "json_dict": context_output.json_dict,
                    "output_format": context_output.output_format,
                    "agent": context_output.agent,
                },
                "inputs": {"name": "John"},
            },
            {
                "task_id": str(task2.id),
                "output": {
                    "description": "Test Task Output",
                    "summary": None,
                    "raw": "test raw output",
                    "pydantic": None,
                    "json_dict": {},
                    "output_format": "json",
                    "agent": "test_agent",
                },
                "inputs": {"name": "John"},
            },
        ],
    ):
        crew.replay(str(task2.id))

        # Check if the first task's output was set correctly
        assert crew.tasks[0].output is not None
        assert isinstance(crew.tasks[0].output, TaskOutput)
        assert crew.tasks[0].output.description == "Context Task Output"
        assert crew.tasks[0].output.agent == "test_agent"
        assert crew.tasks[0].output.raw == "context raw output"
        assert crew.tasks[0].output.output_format == OutputFormat.RAW

        assert crew.tasks[1].prompt_context == "context raw output"


def test_key():
    tasks = [
        Task(
            description="Give me a list of 5 interesting ideas to explore for na article, what makes them unique and interesting.",
            expected_output="Bullet point list of 5 important events.",
            agent=researcher,
        ),
        Task(
            description="Write a 1 amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
            expected_output="A 4 paragraph article about AI.",
            agent=writer,
        ),
    ]
    crew = Crew(
        agents=[researcher, writer],
        process=Process.sequential,
        tasks=tasks,
    )
    hash = hashlib.md5(
        f"{researcher.key}|{writer.key}|{tasks[0].key}|{tasks[1].key}".encode()
    ).hexdigest()

    assert crew.key == hash


def test_conditional_task_requirement_breaks_when_singular_conditional_task():
    def condition_fn(output) -> bool:
        return output.raw.startswith("Andrew Ng has!!")

    task = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        condition=condition_fn,
    )

    with pytest.raises(pydantic_core._pydantic_core.ValidationError):
        Crew(
            agents=[researcher, writer],
            tasks=[task],
        )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_conditional_task_last_task_when_conditional_is_true():
    def condition_fn(output) -> bool:
        return True

    task1 = Task(
        description="Say Hi",
        expected_output="Hi",
        agent=researcher,
    )
    task2 = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        condition=condition_fn,
        agent=writer,
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task1, task2],
    )
    result = crew.kickoff()
    assert result.raw.startswith(
        "1. **The Role of AI Agents in Revolutionizing Customer Service**"
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_conditional_task_last_task_when_conditional_is_false():
    def condition_fn(output) -> bool:
        return False

    task1 = Task(
        description="Say Hi",
        expected_output="Hi",
        agent=researcher,
    )
    task2 = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        condition=condition_fn,
        agent=writer,
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task1, task2],
    )
    result = crew.kickoff()
    assert result.raw == "Hi"


def test_conditional_task_requirement_breaks_when_task_async():
    def my_condition(context):
        return context.get("some_value") > 10

    task = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        execute_async=True,
        condition=my_condition,
        agent=researcher,
    )
    task2 = Task(
        description="Say Hi",
        expected_output="Hi",
        agent=writer,
    )

    with pytest.raises(pydantic_core._pydantic_core.ValidationError):
        Crew(
            agents=[researcher, writer],
            tasks=[task, task2],
        )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_conditional_should_skip():
    task1 = Task(description="Return hello", expected_output="say hi", agent=researcher)

    condition_mock = MagicMock(return_value=False)
    task2 = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        condition=condition_mock,
        agent=writer,
    )
    crew_met = Crew(
        agents=[researcher, writer],
        tasks=[task1, task2],
    )
    with patch.object(Task, "execute_sync") as mock_execute_sync:
        mock_execute_sync.return_value = TaskOutput(
            description="Task 1 description",
            raw="Task 1 output",
            agent="Researcher",
        )

        result = crew_met.kickoff()
        assert mock_execute_sync.call_count == 1

        assert condition_mock.call_count == 1
        assert condition_mock() is False

        assert task2.output is None
        assert result.raw.startswith("Task 1 output")


@pytest.mark.vcr(filter_headers=["authorization"])
def test_conditional_should_execute():
    task1 = Task(description="Return hello", expected_output="say hi", agent=researcher)

    condition_mock = MagicMock(
        return_value=True
    )  # should execute this conditional task
    task2 = ConditionalTask(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        condition=condition_mock,
        agent=writer,
    )
    crew_met = Crew(
        agents=[researcher, writer],
        tasks=[task1, task2],
    )
    with patch.object(Task, "execute_sync") as mock_execute_sync:
        mock_execute_sync.return_value = TaskOutput(
            description="Task 1 description",
            raw="Task 1 output",
            agent="Researcher",
        )

        crew_met.kickoff()

        assert condition_mock.call_count == 1
        assert condition_mock() is True
        assert mock_execute_sync.call_count == 2


@mock.patch("crewai.crew.CrewEvaluator")
@mock.patch("crewai.crew.Crew.kickoff")
def test_crew_testing_function(mock_kickoff, crew_evaluator):
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher],
        tasks=[task],
    )
    n_iterations = 2
    crew.test(n_iterations, openai_model_name="gpt-4o-mini", inputs={"topic": "AI"})

    assert len(mock_kickoff.mock_calls) == n_iterations
    mock_kickoff.assert_has_calls(
        [mock.call(inputs={"topic": "AI"}), mock.call(inputs={"topic": "AI"})]
    )

    crew_evaluator.assert_has_calls(
        [
            mock.call(crew, "gpt-4o-mini"),
            mock.call().set_iteration(1),
            mock.call().set_iteration(2),
            mock.call().print_crew_evaluation_result(),
        ]
    )


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_verbose_manager_agent():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
        verbose=True,
    )

    crew.kickoff()

    assert crew.manager_agent is not None
    assert crew.manager_agent.verbose


@pytest.mark.vcr(filter_headers=["authorization"])
def test_hierarchical_verbose_false_manager_agent():
    task = Task(
        description="Come up with a list of 5 interesting ideas to explore for an article, then write one amazing paragraph highlight for each idea that showcases how good an article about this topic could be. Return the list of ideas with their paragraph and your notes.",
        expected_output="5 bullet points with a paragraph for each idea.",
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[task],
        process=Process.hierarchical,
        manager_llm="gpt-4o",
        verbose=False,
    )

    crew.kickoff()

    assert crew.manager_agent is not None
    assert not crew.manager_agent.verbose
