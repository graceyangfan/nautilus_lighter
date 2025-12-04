#!/usr/bin/env bash
# Example templates for wiring lighter-go into LIGHTER_SIGNER_CMD contract.
#
# 1) Point SIGNER_CMD to the adapter script:
#    export LIGHTER_SIGNER_CMD="python scripts/lighter_go_signer_adapter.py"
# 2) Point to your lighter-go binary:
#    export LIGHTER_GO_BIN=/usr/local/bin/lighter-go
# 3) Define templates matching your binary CLI parameters:

export LIGHTER_GO_TEMPLATE_CREATE_AUTH='%(bin)s create-auth \
  --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
  --private-key %(private_key)s --deadline %(deadline_seconds)d'

export LIGHTER_GO_TEMPLATE_SIGN_CREATE_ORDER='%(bin)s sign-create-order \
  --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
  --private-key %(private_key)s --market-index %(market_index)d --client-order-index %(client_order_index)d \
  --base-amount %(base_amount)d --price %(price)d --is-ask %(is_ask)d --order-type %(order_type)d \
  --time-in-force %(time_in_force)d --reduce-only %(reduce_only)d --trigger-price %(trigger_price)d \
  --order-expiry %(order_expiry)d --nonce %(nonce)s'

export LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ORDER='%(bin)s sign-cancel-order \
  --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
  --private-key %(private_key)s --market-index %(market_index)d --order-index %(order_index)d \
  --nonce %(nonce)s'

export LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ALL='%(bin)s sign-cancel-all \
  --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
  --private-key %(private_key)s --time-in-force %(time_in_force)d --time %(time)d \
  --nonce %(nonce)s'

export LIGHTER_GO_TEMPLATE_SIGN_CREATE_TP_LIMIT='%(bin)s sign-create-tp-limit \
  --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
  --private-key %(private_key)s --market-index %(market_index)d --client-order-index %(client_order_index)d \
  --base-amount %(base_amount)d --tp-price %(tp_price)d --is-ask %(is_ask)d --reduce-only %(reduce_only)d \
  --trigger-price %(trigger_price)d --nonce %(nonce)s'

echo "Templates exported for lighter-go. Remember to 'source' this file."

