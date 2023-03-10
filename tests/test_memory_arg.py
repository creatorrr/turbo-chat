# flake8: noqa
from ward import test

from turbo_chat import *


@test("contains returns True when memory injection works")
async def test_turbo():
    @turbo()
    async def example(context, memory):
        yield System(content="You are a fortune teller")
        yield Generate()

        messages = await memory.get()
        assert len(messages)

    b = example({"zodiac": "pisces"})
    await run(b)
