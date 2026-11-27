# Stage 1: Compile the native C discrete-event engine
FROM gcc:13-bookworm AS c-builder

WORKDIR /build
COPY native/smart_sim.c ./native/smart_sim.c
RUN gcc -O3 -fPIC -shared -Wall -Wextra native/smart_sim.c -o native/smart_sim.so -lm

# Stage 2: Minimal production Python runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md* ./
COPY src ./src
COPY --from=c-builder /build/native/smart_sim.so ./native/smart_sim.so
COPY native/smart_sim.c ./native/smart_sim.c

RUN pip install --upgrade pip && pip install .

# Non-root runtime user
RUN useradd --create-home --shell /bin/bash netsim && chown -R netsim:netsim /app
USER netsim

EXPOSE 9000/udp 9100/tcp

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/health', timeout=2)" || exit 1

ENTRYPOINT ["netsim"]
CMD ["agent", "--bind", "0.0.0.0:9000", "--metrics-port", "9100", "--duration", "86400"]