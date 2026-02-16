#!/usr/bin/env python3
"""Fake log generator that mimics ozon-parser output."""

import random
import time
from datetime import datetime

# Sample articles and queries
ARTICLES = [
    "1868801990", "1213391560", "2732510409", "2588085304",
    "1456789012", "9876543210", "1122334455", "5544332211",
]

QUERIES = [
    "обои в спальню", "обои на кухню", "обои", "парфюмированный спрей для тела",
    "тонкий лонгслив женский", "кардиган мужской", "платье летнее",
    "кроссовки мужские", "сумка женская", "наушники беспроводные",
]

SHORT_IDS = ["1OOQTq4m", "1OVg-7-p", "1XLPl2Ka", "1Il2CqiA"]


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_debug(msg: str) -> None:
    print(f"{timestamp()} | DEBUG    | {msg}")


def log_info(msg: str) -> None:
    print(f"{timestamp()} | INFO     | {msg}")


def generate_scroll_logs(count: int = 5) -> None:
    """Generate random scroll debug logs."""
    for _ in range(count):
        scroll_num = random.randint(1, 150)
        added = random.choice([4, 8, 12, 16, 28])
        total = scroll_num * 12 + random.randint(-20, 20)
        if total < 24:
            total = 24
        log_debug(f"Scroll {scroll_num}: +{added} (total: {total})")
        time.sleep(random.uniform(0.1, 0.5))


def generate_search_start(task_num: int, total: int, article: str, query: str) -> None:
    """Generate search start logs."""
    log_debug(f"[W0] [{task_num}/{total}] {article}: {query}")
    log_info(f"Search: {query} -> {article}")


def generate_found(article: str, position: int, short_id: str) -> None:
    """Generate found position logs."""
    log_info(f"Found {article} at position {position}")
    log_info(f"[{short_id}] {article}: {position}")


def generate_not_found(article: str, short_id: str, max_pos: int = 1000) -> None:
    """Generate not found logs."""
    log_info(f"[{short_id}] {article}: {max_pos}+ (не найден)")


def main() -> None:
    print(f"{timestamp()} | INFO     | Starting fake log generator...")
    print(f"{timestamp()} | INFO     | Logging to logs/fake.log")

    task_num = 1
    total_tasks = random.randint(50, 200)

    while True:
        # Generate some scroll logs
        generate_scroll_logs(random.randint(3, 15))

        # Occasionally generate search/found events
        if random.random() < 0.3:
            article = random.choice(ARTICLES)
            query = random.choice(QUERIES)
            short_id = random.choice(SHORT_IDS)

            generate_search_start(task_num, total_tasks, article, query)
            time.sleep(random.uniform(0.2, 0.8))

            # 70% chance to find, 30% not found
            if random.random() < 0.7:
                position = random.randint(1, 500)
                generate_scroll_logs(random.randint(2, 8))
                generate_found(article, position, short_id)
            else:
                generate_scroll_logs(random.randint(10, 30))
                generate_not_found(article, short_id)

            task_num += 1
            if task_num > total_tasks:
                task_num = 1
                total_tasks = random.randint(50, 200)
                log_info(f"[{short_id}] Done: {total_tasks}/{total_tasks}")
                time.sleep(2)
                log_info(f"Starting tracker for {random.randint(1, 5)} spreadsheets")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{timestamp()} | INFO     | Stopped")
