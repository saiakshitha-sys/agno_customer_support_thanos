
Performance on Agent Instantiation with Tool- performace
Reliability with Multiple Tools- reliability
Accuracy with Given Answer- accuracy


> ## Documentation Index
> Fetch the complete documentation index at: https://docs.agno.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Accuracy with Given Answer

> Example showing how to evaluate the accuracy of an Agno Agent's response with a given answer.

<Steps>
  <Step title="Create a Python file">
    ```python accuracy_with_given_answer.py theme={null}
    from typing import Optional

    from agno.eval.accuracy import AccuracyEval, AccuracyResult
    from agno.models.openai import OpenAIResponses

    evaluation = AccuracyEval(
        name="Given Answer Evaluation",
        model=OpenAIResponses(id="gpt-5.2"),
        input="What is 10*5 then to the power of 2? do it step by step",
        expected_output="2500",
    )
    result_with_given_answer: Optional[AccuracyResult] = evaluation.run_with_output(
        output="2500", print_results=True
    )
    assert result_with_given_answer is not None and result_with_given_answer.avg_score >= 8
    ```
  </Step>

  <Snippet file="create-venv-step.mdx" />

  <Step title="Install dependencies">
    ```bash  theme={null}
    uv pip install -U openai agno
    ```
  </Step>

  <Step title="Export your OpenAI API key">
    <CodeGroup>
      ```bash Mac/Linux theme={null}
        export OPENAI_API_KEY="your_openai_api_key_here"
      ```

      ```bash Windows theme={null}
        $Env:OPENAI_API_KEY="your_openai_api_key_here"
      ```
    </CodeGroup>
  </Step>

  <Step title="Run Agent">
    ```bash  theme={null}
    python accuracy_with_given_answer.py
    ```
  </Step>
</Steps>



# Performance on Agent Instantiation with Tool- performace

> Example showing how to evaluate the performance of an Agno Agent's response with a given answer.

<Steps>
  <Step title="Create a Python file">
    ```python performance_on_agent_instantiation_with_tool.py theme={null}
    from typing import Optional

    from agno.eval.performance import PerformanceEval, PerformanceResult
    from agno.models.openai import OpenAIResponses

    evaluation = PerformanceEval(
        name="Performance Evaluation",
        model=OpenAIResponses(id="gpt-5.2"),
        input="What is 10*5 then to the power of 2? do it step by step",
        expected_output="2500",
    )
    result_with_given_answer: Optional[PerformanceResult] = evaluation.run_with_output(
        output="2500", print_results=True
    )
    assert result_with_given_answer is not None and result_with_given_answer.avg_score >= 8
    ```
  </Step>

  <Snippet file="create-venv-step.mdx" />

  <Step title="Install dependencies">
    ```bash  theme={null}
    uv pip install -U openai agno
    ```
  </Step>

  <Step title="Export your OpenAI API key">
    <CodeGroup>
      ```bash Mac/Linux theme={null}
        export OPENAI_API_KEY="your_openai_api_key_here"
      ```

      ```bash Windows theme={null}
        $Env:OPENAI_API_KEY="your_openai_api_key_here"
      ```
    </CodeGroup>
  </Step>

  <Step title="Run Agent">
    ```bash  theme={null}
    python performance_on_agent_instantiation_with_tool.py
    ```
  </Step>
</Steps>


# Reliability with Multiple Tools- reliability

> Example showing how to evaluate the reliability of an Agno Agent's response with a given answer.

<Steps>
  <Step title="Create a Python file">
    ```python reliability_with_multiple_tools.py theme={null}
    from typing import Optional

    from agno.eval.reliability import ReliabilityEval, ReliabilityResult
    from agno.models.openai import OpenAIResponses

    evaluation = ReliabilityEval(
        name="Reliability Evaluation",
        model=OpenAIResponses(id="gpt-5.2"),
        input="What is 10*5 then to the power of 2? do it step by step",
        expected_output="2500",
    )
    result_with_given_answer: Optional[ReliabilityResult] = evaluation.run_with_output(
        output="2500", print_results=True
    )
    assert result_with_given_answer is not None and result_with_given_answer.avg_score >= 8
    ```
  </Step>

  <Snippet file="create-venv-step.mdx" />

  <Step title="Install dependencies">
    ```bash  theme={null}
    uv pip install -U openai agno
    ```
  </Step>

  <Step title="Export your OpenAI API key">
    <CodeGroup>
      ```bash Mac/Linux theme={null}
        export OPENAI_API_KEY="your_openai_api_key_here"
      ```

      ```bash Windows theme={null}
        $Env:OPENAI_API_KEY="your_openai_api_key_here"
      ```
    </CodeGroup>
  </Step>

  <Step title="Run Agent">
    ```bash  theme={null}
    python reliability_with_multiple_tools.py
    ```
  </Step>
</Steps>
