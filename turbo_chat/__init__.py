from abc import ABC, abstractmethod
from functools import wraps
import os
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)

from aiohttp import ClientSession
import openai
import pydantic

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


__all__ = [
    "System",
    "User",
    "Assistant",
    "ExampleUser",
    "ExampleAssistant",
    "Generate",
    "GetUserInput",
    "InvalidValueYieldedError",
    "GeneratorAlreadyExhaustedError",
    "TurboGen",
    "TurboGenStr",
    "BasePrefixMessageCollection",
    "BaseMemory",
    "Example",
    "turbo",
    "run",
]


# Set up openai credentials
openai.organization = os.getenv("OPENAI_ORGANIZATION", None)
openai.api_key = os.environ["OPENAI_API_KEY"]


# Enums
# Allowed values for openai chatml prefixes
MessageRole = Literal[
    "system",
    "user",
    "assistant",
    "system name=example_user",
    "system name=example_assistant",
]

# Allowed chatgpt model names
TurboModel = Literal[
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-0301",
]


# Models
class PrefixMessage(pydantic.BaseModel):
    """Container for a single chatml prefix message"""

    role: MessageRole
    content: str


class System(PrefixMessage):
    """System message"""

    role: MessageRole = "system"


class User(PrefixMessage):
    """User message"""

    role: MessageRole = "user"


class Assistant(PrefixMessage):
    """Assistant message"""

    role: MessageRole = "assistant"


class ExampleUser(PrefixMessage):
    """User example message"""

    role: MessageRole = "system name=example_user"


class ExampleAssistant(PrefixMessage):
    """Assistant example message"""

    role: MessageRole = "system name=example_assistant"


# Utility classes
class Generate(pydantic.BaseModel):
    """Placeholder value to indicate that completion should be run"""

    settings: Dict[str, Any] = {}


class GetUserInput(pydantic.BaseModel):
    """Placeholder value to indicate that user input is needed"""

    message: str = "User input needed"


class GeneratorAlreadyExhausted(pydantic.BaseModel):
    """Placeholder value to indicate that the generator was already exhausted"""

    ...


class InvalidValueYieldedError(ValueError):
    """Invalid value yielded by generator"""

    ...


class GeneratorAlreadyExhaustedError(StopAsyncIteration):
    """Generator was already exhausted"""

    ...


# Types
Context = Dict[str, Any]
TurboGen = AsyncGenerator[PrefixMessage, Any]
TurboGenStr = AsyncGenerator[Union[str, GetUserInput], Any]
TurboGenFn = Callable[[Context], TurboGen]
TurboGenStrFn = Callable[[Context], TurboGenStr]


# Abstract classes
class BasePrefixMessageCollection(ABC):
    """Base class for async collections of prefix messages"""

    @abstractmethod
    async def get(self) -> List[PrefixMessage]:
        ...

    async def get_dicts(self) -> List[Dict[str, str]]:
        messages = await self.get()
        return [message.dict() for message in messages]


class BaseMemory(BasePrefixMessageCollection):
    """Base class for interface for persisting prefix messages for a session"""

    async def init(self, context: Context) -> None:
        ...

    @abstractmethod
    async def append(self, item: PrefixMessage) -> None:
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...

    async def extend(self, items: List[PrefixMessage]) -> None:
        for item in items:
            await self.append(item)


# Abstract implementations
class Example(pydantic.BaseModel, BasePrefixMessageCollection):
    user: str
    assistant: str

    async def get(self) -> List[PrefixMessage]:
        return [
            ExampleUser(content=self.user),
            ExampleAssistant(content=self.assistant),
        ]


class ListMemory(BaseMemory, pydantic.BaseModel):
    """Store messages in an in-memory list"""

    messages: List[PrefixMessage] = []

    async def get(self) -> List[PrefixMessage]:
        return [
            message for message in self.messages if isinstance(message, PrefixMessage)
        ]

    async def append(self, item) -> None:
        self.messages.append(item)

    async def clear(self) -> None:
        self.messages = []


# Retries
def create_retry_decorator(
    min_seconds: int = 4,
    max_seconds: int = 10,
    max_retries: int = 5,
) -> Callable[[Any], Any]:
    # Wait 2^x * 1 second between each retry starting with
    # 4 seconds, then up to 10 seconds, then 10 seconds afterwards
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=min_seconds, max=max_seconds),
        retry=(
            retry_if_exception_type(openai.error.Timeout)
            | retry_if_exception_type(openai.error.APIError)
            | retry_if_exception_type(openai.error.APIConnectionError)
            | retry_if_exception_type(openai.error.RateLimitError)
            | retry_if_exception_type(openai.error.ServiceUnavailableError)
        ),
    )


# Decorator
def turbo(
    memory: Optional[BaseMemory] = None,
    model: TurboModel = "gpt-3.5-turbo",
    stream: bool = False,
    **kwargs,
) -> Callable[[TurboGenFn], TurboGenStrFn]:
    """Parameterized decorator for creating a chatml app from an async generator"""

    # Prepare openai args
    chat_completion_args = {
        **kwargs,
        "model": model,
        "stream": stream,
    }

    # Get or create memory
    if memory is None:
        memory = ListMemory()

    # Create tenacity retry decorator
    with_retries = create_retry_decorator()

    # Chat runner
    @with_retries
    async def run_chat(memory: BaseMemory, **kwargs) -> str:
        """Run ChatCompletion for the memory so far"""

        # Get messages from memory
        messages = await memory.get_dicts()

        # Create completion
        args = {
            **chat_completion_args,
            **kwargs,
        }

        chat_completion = await openai.ChatCompletion.acreate(
            messages=messages,
            **args,
        )

        # Parse result
        output = chat_completion.choices[0].message
        content = output["content"]
        result = Assistant(content=content)

        # Append result to memory
        await memory.append(result)

        return content

    # Parameterized decorator fn
    def wrap_turbo_gen_fn(gen_fn: TurboGenFn) -> TurboGenStrFn:
        """Wrapper for chatml app async generator"""

        @wraps(gen_fn)
        async def turbo_gen_fn(context: Context) -> TurboGenStr:
            """Wrapped chatml app from an async generator"""

            # Init
            await memory.init(context)
            turbo_gen = gen_fn(context)

            # Parameters
            payload: Any = None
            already_yielded: bool = False

            try:
                # Create a new aiohttp session for this
                openai.aiosession.set(ClientSession())

                while True:
                    # Step through the wrapped generator
                    output = await turbo_gen.asend(payload)
                    payload = None
                    already_yielded = False

                    # Add to memory
                    if isinstance(output, PrefixMessage):
                        await memory.append(output)

                    elif isinstance(output, BasePrefixMessageCollection):
                        await memory.extend(output)

                    # Yield to user if GetUserInput
                    elif isinstance(output, GetUserInput):
                        payload = yield output
                        assert payload, f"User input was required, {payload} passed"

                    # Generate and yield result
                    elif isinstance(output, Generate):
                        payload = await run_chat(memory)
                        yield payload

                        already_yielded = True

                    else:
                        raise InvalidValueYieldedError(
                            f"Invalid value yielded by generator: {type(output)}"
                        )

            except StopAsyncIteration:
                # Generator over, yield result if not already yielded
                if not already_yielded:
                    payload = await run_chat(memory)
                    yield payload

            finally:
                # close the http session
                session = openai.aiosession.get()
                session and (await session.close())

        return turbo_gen_fn

    return wrap_turbo_gen_fn


async def run(
    gen: TurboGenStr,
    input: Optional[str] = None,
) -> Tuple[Union[str, GetUserInput], bool]:
    """Run a turbo app"""

    # Set placeholder values
    done = False
    output = GeneratorAlreadyExhausted()

    # Run generator
    try:
        while not isinstance(output := await gen.asend(input), GetUserInput):
            pass

    # Generator exhausted, mark done
    except StopAsyncIteration:
        done = True

    # Output is still placeholder? Raise error
    if isinstance(output, GeneratorAlreadyExhausted):
        raise GeneratorAlreadyExhaustedError()

    return (output, done)