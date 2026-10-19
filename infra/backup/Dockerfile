# On-demand backup/restore tooling: pg_dump/pg_restore matching the server major + age encryption.
FROM postgres:18.6-alpine
# gosu (Go binary used only by the server entrypoint) is unused here; drop it and apply Alpine fixes.
RUN apk add --no-cache age=~1 jq && apk upgrade --no-cache && rm -f /usr/local/bin/gosu
USER postgres
ENTRYPOINT []
