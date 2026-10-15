import argparse
import asyncio
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langchain_core.messages import AIMessage, HumanMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

from src.telegram import telegram_bot
from src.agent.agent import agent

# short term memory, all chat messages, max 30k tokens
chat_history = []
MAX_HISTORY_TOKENS = 30_000
REPORT_HOURS = os.getenv("REPORT_HOURS")

REPORT_PROMPT = (
    "Execute the `run_log_extraction` tool. Analyze the retrieved logs and generate a summary "
    "grouping unique errors, their frequencies, file locations, and suggested fixes and query tunings. "
    "Use the `memory` annotations in the result to lead with new and regressed errors, note known "
    "recurring ones and volume shifts briefly, and mention resolved groups. "
)

_agent_lock = asyncio.Lock()


def _trim_history() -> None:
    chat_history[:] = trim_messages(
        chat_history,
        token_counter=count_tokens_approximately, # chars/4
        strategy="last",
        max_tokens=MAX_HISTORY_TOKENS,
        start_on="human",
        allow_partial=False,
    )


async def ask_agent(prompt: str) -> str:
    """handles chat messages from telegram"""
    async with _agent_lock:
        result = await agent.ainvoke({"messages": chat_history + [HumanMessage(content=prompt)]})
        answer = result["messages"][-1].content
        chat_history.append(HumanMessage(content=prompt))
        chat_history.append(AIMessage(content=answer))
        _trim_history()
        return answer


async def run_log_analysis_and_report():
    """triggered by schedule, arg --once or telegram command /report."""
    print("Running error analysis...")

    analysis_text = await ask_agent(REPORT_PROMPT)
    await telegram_bot.send(analysis_text)


async def run_scheduled():
    app = await telegram_bot.start_polling(
        on_message=ask_agent, on_report=run_log_analysis_and_report
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_log_analysis_and_report, trigger="cron", hour=REPORT_HOURS)
    scheduler.start()

    print(f"Dr. Sre active. Scheduled runs at hours {REPORT_HOURS} local")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await telegram_bot.stop_polling(app)


def main():
    asyncio.run(run_scheduled())


async def run_once():
    try:
        await run_log_analysis_and_report()
    finally:
        await telegram_bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dr. Sre")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single analysis cycle now, post it to Telegram (for testing)",
    )
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
    else:
        main()
