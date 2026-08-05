# Production image for the Mathiqo QB serving API (tools/rest_server.py).
#
# The bank is BUILT INTO THE IMAGE rather than mounted. A bank build is a pure
# function of the promoted templates plus the locale pack, and every served item
# carries the bank_version it came from, so baking it in makes the image and the
# content it serves one immutable, reproducible artifact — you can always answer
# "which items was this container serving?" from the image tag alone. Mounting a
# bank directory would let the content drift out from under a running server
# with nothing in the deploy history to show it.
#
# Python >=3.10 is required by the `mcp` SDK (see README).
FROM python:3.12-slim AS build
WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build and activate the bank at image-build time. The build hard-fails on a
# malformed template, so a broken content change cannot produce a running
# container that serves it.
ARG BANK_VERSION=v1
RUN python tools/bank_build.py \
        --templates-dir content/templates \
        --out banks \
        --bank-version "${BANK_VERSION}" \
 && python tools/bank_activate.py --banks banks --bank-version "${BANK_VERSION}"

FROM python:3.12-slim AS run
WORKDIR /app

# Run unprivileged. The server needs to write only the attempt log.
RUN useradd --create-home --uid 10001 mathiqo

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /build /app

RUN mkdir -p /app/logs && chown -R mathiqo:mathiqo /app/logs
USER mathiqo

EXPOSE 8000

# Bind 0.0.0.0 so the app container can reach this over the compose network.
# That is safe ONLY because the port is never published to the host — see the
# comment on the `qb` service in deploy/do/docker-compose.yml. The server's own
# default is loopback for exactly this reason; overriding it is a deployment
# decision, made here, once, next to the network policy that justifies it.
CMD ["python", "tools/rest_server.py", \
     "--banks-dir", "banks", \
     "--logs-dir", "logs", \
     "--content", "content/fractions", \
     "--credentials-file", "/run/secrets/qb-credentials.json", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
