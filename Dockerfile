FROM python:3.12-slim

WORKDIR /app

# Copy dependency list first to leverage Docker layer cache
COPY requirements.txt .

# Single RUN layer: install system deps, Python packages, and Playwright chromium
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libatspi2.0-0 \
        tzdata \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# 时区：项目的统计口径是**北京日历日**（issue #77）。容器默认 UTC 会让"本地日"退化为
# UTC 日，`local_day_start()` / `date(...,'localtime')` 全部失效，统计与日报会错格。
# tzdata 是前提 —— 只设 TZ 而不装它，libc 找不到 /usr/share/zoneinfo 仍会回退 UTC。
# 放在大 RUN 之后，避免改动 ENV 让上面那层缓存失效。
ENV TZ=Asia/Shanghai

# Copy project source
COPY . .

CMD ["python3", "-m", "src.cli", "-c", "etc/config.json"]
