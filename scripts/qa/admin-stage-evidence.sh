#!/usr/bin/env bash

# Persist only bounded, secret-free administration-stage results. Response bodies
# stay in the caller's temporary client directory and are used only to extract a
# validated safe error code when the expected contract is not met.
write_admin_stage_evidence() {
    local evidence_dir=$1
    local stage=$2
    local expected_http=$3
    local observed_http=$4
    local contract_valid=$5
    local response_file=$6
    local observed_http_json=null
    local safe_error_code=unavailable
    local stage_status=FAIL
    local extracted_code=""
    local temporary_path="$evidence_dir/.admin-stage-${stage}.json.$$"

    if [[ "$observed_http" =~ ^[1-5][0-9][0-9]$ ]]; then
        observed_http_json=$observed_http
    fi
    if [ "$contract_valid" = true ]; then
        safe_error_code=none
        stage_status=PASS
    elif [ -s "$response_file" ]; then
        extracted_code=$(jq -er '
            if type == "object"
                and (.error | type == "object")
                and (.error.code | type == "string" and test("^[a-z_]+$"))
            then .error.code
            else empty
            end
        ' "$response_file" 2>/dev/null) || extracted_code=""
        if [[ "$extracted_code" =~ ^[a-z_]+$ ]]; then
            safe_error_code=$extracted_code
        fi
    fi

    jq -cn \
        --arg stage "$stage" \
        --argjson expected_http "$expected_http" \
        --argjson observed_http "$observed_http_json" \
        --argjson contract_valid "$contract_valid" \
        --arg safe_error_code "$safe_error_code" \
        --arg status "$stage_status" \
        '{schema_version:1,stage:$stage,expected_http:$expected_http,observed_http:$observed_http,contract_valid:$contract_valid,safe_error_code:$safe_error_code,status:$status}' \
        > "$temporary_path"
    chmod 0444 "$temporary_path"
    mv -- "$temporary_path" "$evidence_dir/admin-stage-${stage}.json"
}

assert_admin_stage_evidence() {
    local evidence_dir=$1
    local stage=$2
    local expected_http=$3

    jq -e \
        --arg stage "$stage" \
        --argjson expected_http "$expected_http" \
        '. == {schema_version:1,stage:$stage,expected_http:$expected_http,observed_http:$expected_http,contract_valid:true,safe_error_code:"none",status:"PASS"}' \
        "$evidence_dir/admin-stage-${stage}.json" >/dev/null
}
