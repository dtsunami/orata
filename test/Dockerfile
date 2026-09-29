# test/Dockerfile -- throwaway verification rig. NOT a deployment target.
#
# Same base and same Asterisk package as the Pi (Debian 12 -> Asterisk 20),
# so config parsing and astdb behaviour here mean something. Networking
# does NOT mean anything: see test/README.md.
#
#   docker build -t orata-test -f test/Dockerfile .
#   docker run --rm -v "%cd%:/repo:ro" orata-test
#
FROM debian:12

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk \
        curl \
        jq \
        python3 \
        espeak \
        sox \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copied in (not mounted) and de-CRLF'd at build time -- a mounted script
# authored on Windows cannot bootstrap itself.
COPY test/run-tests.sh /run-tests.sh
RUN sed -i 's/\r$//' /run-tests.sh && chmod 755 /run-tests.sh

ENTRYPOINT ["/run-tests.sh"]