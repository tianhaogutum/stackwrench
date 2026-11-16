secret := 'hidden-variable-value'
# This description must not enter DOT
release: build test build
    touch forbidden
build: shared common shared
    printf '%s' '{{secret}}'
independent:
test: common shared
common: shared
shared:
