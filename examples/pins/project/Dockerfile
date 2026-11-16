# Synthetic text fixture; do not build it.
FROM --platform=linux/amd64 node:20.11.0-alpine AS app # retain this comment
WORKDIR /app
FROM python:3.12.8-slim-bookworm AS python_runtime
FROM golang:1.23.4-alpine3.21 AS go_build
FROM app AS final # previous stage, not an external image
