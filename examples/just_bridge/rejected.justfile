# Deliberately unsupported: conversion must not execute any of this.
revision := `git rev-parse HEAD`
set shell := ['bash', '-eu', '-c']

[script]
release target='production':
    #!/usr/bin/env bash
    echo "would deploy {{target}} at {{revision}}"
