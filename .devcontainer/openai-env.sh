#!/bin/sh
# Source this file. It does not call the network.
# A set OPENAI_BASE_URL secret (copied to OPENAI_BASE_URL_SECRET by the
# devcontainer) replaces the Carrie White default. LLAMA_API_KEY fills
# OPENAI_API_KEY when that variable is empty.

if [ -n "${OPENAI_BASE_URL_SECRET:-}" ]; then
  OPENAI_BASE_URL="${OPENAI_BASE_URL_SECRET}"
elif [ -z "${OPENAI_BASE_URL:-}" ]; then
  OPENAI_BASE_URL="https://chat.srv1536099.hstgr.cloud/v1"
fi

if [ -z "${OPENAI_MODEL:-}" ]; then
  OPENAI_MODEL="Llama-3.2-3B-Instruct"
fi

if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${LLAMA_API_KEY:-}" ]; then
  OPENAI_API_KEY="${LLAMA_API_KEY}"
fi

export OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY
