"""ReAct step engine."""

import uuid
from itertools import chain
from threading import Thread
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    cast,
)

from llama_index.agent.react.formatter import ReActChatFormatter
from llama_index.agent.react.output_parser import ReActOutputParser
from llama_index.agent.react.types import (
    ActionReasoningStep,
    BaseReasoningStep,
    ObservationReasoningStep,
    ResponseReasoningStep,
)
from llama_index.agent.types import BaseAgent
from llama_index.callbacks import (
    CallbackManager,
    CBEventType,
    EventPayload,
    trace_method,
)
from llama_index.chat_engine.types import AgentChatResponse, StreamingAgentChatResponse
from llama_index.llms.base import LLM, ChatMessage, ChatResponse, MessageRole
from llama_index.llms.openai import OpenAI
from llama_index.memory.chat_memory_buffer import ChatMemoryBuffer
from llama_index.memory.types import BaseMemory
from llama_index.objects.base import ObjectRetriever
from llama_index.tools import BaseTool, ToolOutput, adapt_to_async_tool
from llama_index.tools.types import AsyncBaseTool
from llama_index.utils import print_text, unit_generator
from llama_index.agent.v1.schema import (
    BaseAgentStepEngine,
    Task,
    TaskStep,
    TaskStepOutput,
)
import uuid

DEFAULT_MODEL_NAME = "gpt-3.5-turbo-0613"


class ReActAgentStepEngine(BaseAgentStepEngine):
    """OpenAI Agent step engine."""

    def __init__(
        self,
        tools: Sequence[BaseTool],
        llm: LLM,
        max_iterations: int = 10,
        react_chat_formatter: Optional[ReActChatFormatter] = None,
        output_parser: Optional[ReActOutputParser] = None,
        callback_manager: Optional[CallbackManager] = None,
        verbose: bool = False,
        tool_retriever: Optional[ObjectRetriever[BaseTool]] = None,
    ) -> None:
        self._llm = llm
        self.callback_manager = callback_manager or llm.callback_manager
        self._max_iterations = max_iterations
        self._react_chat_formatter = react_chat_formatter or ReActChatFormatter()
        self._output_parser = output_parser or ReActOutputParser()
        self._verbose = verbose

        if len(tools) > 0 and tool_retriever is not None:
            raise ValueError("Cannot specify both tools and tool_retriever")
        elif len(tools) > 0:
            self._get_tools = lambda _: tools
        elif tool_retriever is not None:
            tool_retriever_c = cast(ObjectRetriever[BaseTool], tool_retriever)
            self._get_tools = lambda message: tool_retriever_c.retrieve(message)
        else:
            self._get_tools = lambda _: []

    @classmethod
    def from_tools(
        cls,
        tools: Optional[List[BaseTool]] = None,
        tool_retriever: Optional[ObjectRetriever[BaseTool]] = None,
        llm: Optional[LLM] = None,
        chat_history: Optional[List[ChatMessage]] = None,
        max_iterations: int = 10,
        react_chat_formatter: Optional[ReActChatFormatter] = None,
        output_parser: Optional[ReActOutputParser] = None,
        callback_manager: Optional[CallbackManager] = None,
        verbose: bool = False,
        **kwargs: Any,
    ) -> "ReActAgentStepEngine":
        """Convenience constructor method from set of of BaseTools (Optional).

        NOTE: kwargs should have been exhausted by this point. In other words
        the various upstream components such as BaseSynthesizer (response synthesizer)
        or BaseRetriever should have picked up off their respective kwargs in their
        constructions.

        Returns:
            ReActAgent
        """
        llm = llm or OpenAI(model=DEFAULT_MODEL_NAME)
        if callback_manager is not None:
            llm.callback_manager = callback_manager
        return cls(
            tools=tools or [],
            tool_retriever=tool_retriever,
            llm=llm,
            max_iterations=max_iterations,
            react_chat_formatter=react_chat_formatter,
            output_parser=output_parser,
            callback_manager=callback_manager,
            verbose=verbose,
        )

    def initialize_step(self, task: Task, **kwargs: Any) -> TaskStep:
        """Initialize step from task."""
        sources: List[ToolOutput] = []
        current_reasoning: List[BaseReasoningStep] = []

        # initialize state in this step
        step_state = {
            "sources": sources,
            "current_reasoning": current_reasoning,
        }

        return TaskStep(
            task_id=task.task_id,
            step_id=str(uuid.uuid4()),
            input=task.input,
            memory=task.memory,
            step_state=step_state,
        )

    def get_tools(self, input: str) -> List[BaseTool]:
        """Get tools."""
        return [adapt_to_async_tool(t) for t in self._get_tools(input)]

    def _extract_reasoning_step(
        self, output: ChatResponse, is_streaming: bool = False
    ) -> Tuple[str, List[BaseReasoningStep], bool]:
        """
        Extracts the reasoning step from the given output.

        This method parses the message content from the output,
        extracts the reasoning step, and determines whether the processing is
        complete. It also performs validation checks on the output and
        handles possible errors.
        """
        if output.message.content is None:
            raise ValueError("Got empty message.")
        message_content = output.message.content
        current_reasoning = []
        try:
            reasoning_step = self._output_parser.parse(message_content, is_streaming)
        except BaseException as exc:
            raise ValueError(f"Could not parse output: {message_content}") from exc
        if self._verbose:
            print_text(f"{reasoning_step.get_content()}\n", color="pink")
        current_reasoning.append(reasoning_step)

        if reasoning_step.is_done:
            return message_content, current_reasoning, True

        reasoning_step = cast(ActionReasoningStep, reasoning_step)
        if not isinstance(reasoning_step, ActionReasoningStep):
            raise ValueError(f"Expected ActionReasoningStep, got {reasoning_step}")

        return message_content, current_reasoning, False

    def _process_actions(
        self,
        step: TaskStep,
        tools: Sequence[AsyncBaseTool],
        output: ChatResponse,
        is_streaming: bool = False,
    ) -> Tuple[List[BaseReasoningStep], bool]:
        tools_dict: Dict[str, AsyncBaseTool] = {
            tool.metadata.get_name(): tool for tool in tools
        }
        _, current_reasoning, is_done = self._extract_reasoning_step(
            output, is_streaming
        )

        if is_done:
            return current_reasoning, True

        # call tool with input
        reasoning_step = cast(ActionReasoningStep, current_reasoning[-1])
        tool = tools_dict[reasoning_step.action]
        with self.callback_manager.event(
            CBEventType.FUNCTION_CALL,
            payload={
                EventPayload.FUNCTION_CALL: reasoning_step.action_input,
                EventPayload.TOOL: tool.metadata,
            },
        ) as event:
            tool_output = tool.call(**reasoning_step.action_input)
            event.on_end(payload={EventPayload.FUNCTION_OUTPUT: str(tool_output)})

        step.step_state["sources"].append(tool_output)

        observation_step = ObservationReasoningStep(observation=str(tool_output))
        current_reasoning.append(observation_step)
        if self._verbose:
            print_text(f"{observation_step.get_content()}\n", color="blue")
        return current_reasoning, False

    async def _aprocess_actions(
        self,
        step: TaskStep,
        tools: Sequence[AsyncBaseTool],
        output: ChatResponse,
        is_streaming: bool = False,
    ) -> Tuple[List[BaseReasoningStep], bool]:
        tools_dict = {tool.metadata.name: tool for tool in tools}
        _, current_reasoning, is_done = self._extract_reasoning_step(
            output, is_streaming
        )

        if is_done:
            return current_reasoning, True

        # call tool with input
        reasoning_step = cast(ActionReasoningStep, current_reasoning[-1])
        tool = tools_dict[reasoning_step.action]
        with self.callback_manager.event(
            CBEventType.FUNCTION_CALL,
            payload={
                EventPayload.FUNCTION_CALL: reasoning_step.action_input,
                EventPayload.TOOL: tool.metadata,
            },
        ) as event:
            tool_output = await tool.acall(**reasoning_step.action_input)
            event.on_end(payload={EventPayload.FUNCTION_OUTPUT: str(tool_output)})

        step.step_state["sources"].append(tool_output)

        observation_step = ObservationReasoningStep(observation=str(tool_output))
        current_reasoning.append(observation_step)
        if self._verbose:
            print_text(f"{observation_step.get_content()}\n", color="blue")
        return current_reasoning, False

    def _get_response(
        self,
        current_reasoning: List[BaseReasoningStep],
        sources: List[ToolOutput],
    ) -> AgentChatResponse:
        """Get response from reasoning steps."""
        if len(current_reasoning) == 0:
            raise ValueError("No reasoning steps were taken.")
        elif len(current_reasoning) == self._max_iterations:
            raise ValueError("Reached max iterations.")

        if isinstance(current_reasoning[-1], ResponseReasoningStep):
            response_step = cast(ResponseReasoningStep, current_reasoning[-1])
            response_str = response_step.response
        else:
            response_str = current_reasoning[-1].get_content()

        # TODO: add sources from reasoning steps
        return AgentChatResponse(response=response_str, sources=sources)

    def _infer_stream_chunk_is_final(self, chunk: ChatResponse) -> bool:
        """Infers if a chunk from a live stream is the start of the final
        reasoning step. (i.e., and should eventually become
        ResponseReasoningStep — not part of this function's logic tho.).

        Args:
            chunk (ChatResponse): the current chunk stream to check

        Returns:
            bool: Boolean on whether the chunk is the start of the final response
        """
        latest_content = chunk.message.content
        if latest_content:
            if not latest_content.startswith(
                "Thought"
            ):  # doesn't follow thought-action format
                return True
            else:
                if "Answer: " in latest_content:
                    return True
        return False

    def _add_back_chunk_to_stream(
        self, chunk: ChatResponse, chat_stream: Generator[ChatResponse, None, None]
    ) -> Generator[ChatResponse, None, None]:
        """Helper method for adding back initial chunk stream of final response
        back to the rest of the chat_stream.

        Args:
            chunk (ChatResponse): the chunk to add back to the beginning of the
                                    chat_stream.

        Return:
            Generator[ChatResponse, None, None]: the updated chat_stream
        """
        updated_stream = chain.from_iterable(  # need to add back partial response chunk
            [
                unit_generator(chunk),
                chat_stream,
            ]
        )
        # use cast to avoid mypy issue with chain and Generator
        updated_stream_c: Generator[ChatResponse, None, None] = cast(
            Generator[ChatResponse, None, None], updated_stream
        )
        return updated_stream_c

    async def _async_add_back_chunk_to_stream(
        self, chunk: ChatResponse, chat_stream: AsyncGenerator[ChatResponse, None]
    ) -> AsyncGenerator[ChatResponse, None]:
        """Helper method for adding back initial chunk stream of final response
        back to the rest of the chat_stream.

        NOTE: this itself is not an async function.

        Args:
            chunk (ChatResponse): the chunk to add back to the beginning of the
                                    chat_stream.

        Return:
            AsyncGenerator[ChatResponse, None]: the updated async chat_stream
        """
        yield chunk
        async for item in chat_stream:
            yield item

    def _run_step(
        self,
        step: TaskStep,
        task: Task,
    ) -> TaskStepOutput:
        """Run step."""
        # TODO: see if we want to do step-based inputs
        tools = self.get_tools(task.input)

        input_chat = self._react_chat_formatter.format(
            tools,
            chat_history=step.memory.get(),
            current_reasoning=step.step_state["current_reasoning"],
        )
        # send prompt
        chat_response = self._llm.chat(input_chat)
        # given react prompt outputs, call tools or return response
        reasoning_steps, is_done = self._process_actions(
            step, tools, output=chat_response
        )
        step.step_state["current_reasoning"].extend(reasoning_steps)
        agent_response = self._get_response(
            step.step_state["current_reasoning"], step.step_state["sources"]
        )

        if is_done:
            step.memory.put(
                ChatMessage(content=agent_response.response, role=MessageRole.ASSISTANT)
            )
            new_steps = []
        else:
            new_steps = [
                step.get_next_step(
                    step_id=str(uuid.uuid4()),
                    # NOTE: input is unused
                    input=None,
                )
            ]

        return TaskStepOutput(
            output=agent_response,
            task_step=step,
            is_last=is_done,
            next_steps=new_steps,
        )

    async def _arun_step(
        self,
        step: TaskStep,
        task: Task,
    ) -> TaskStepOutput:
        """Run step."""
        # TODO: see if we want to do step-based inputs
        tools = self.get_tools(task.input)

        input_chat = self._react_chat_formatter.format(
            tools,
            chat_history=step.memory.get(),
            current_reasoning=step.step_state["current_reasoning"],
        )
        # send prompt
        chat_response = await self._llm.achat(input_chat)
        # given react prompt outputs, call tools or return response
        reasoning_steps, is_done = await self._aprocess_actions(
            step, tools, output=chat_response
        )
        step.step_state["current_reasoning"].extend(reasoning_steps)
        agent_response = self._get_response(
            step.step_state["current_reasoning"], step.step_state["sources"]
        )

        if is_done:
            step.memory.put(
                ChatMessage(content=agent_response.response, role=MessageRole.ASSISTANT)
            )
            new_steps = []
        else:
            new_steps = [
                step.get_next_step(
                    step_id=str(uuid.uuid4()),
                    # NOTE: input is unused
                    input=None,
                )
            ]

        return TaskStepOutput(
            output=agent_response,
            task_step=step,
            is_last=is_done,
            next_steps=new_steps,
        )

    def run_step(self, step: TaskStep, task: Task, **kwargs: Any) -> TaskStepOutput:
        """Run step."""
        return self._run_step(step, task)

    async def arun_step(
        self, step: TaskStep, task: Task, **kwargs: Any
    ) -> TaskStepOutput:
        """Run step (async)."""
        return await self._arun_step(step, task)

    def stream_step(self, step: TaskStep, task: Task, **kwargs: Any) -> TaskStepOutput:
        """Run step (stream)."""
        # TODO: figure out if we need a different type for TaskStepOutput
        raise NotImplementedError

    async def astream_step(
        self, step: TaskStep, task: Task, **kwargs: Any
    ) -> TaskStepOutput:
        """Run step (async stream)."""
        raise NotImplementedError