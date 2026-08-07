#!/usr/bin/env python3
"""Validate ordinary inline graduated prompts and one separate admitted plan template."""

import argparse
from copy import deepcopy
import importlib.util
import json
import tempfile
from pathlib import Path


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "assets" / "graduated-route-fixtures.json"
DIRECT_PROMPTS = ["Open Chrome", "Open Chrome and open YouTube", "Open Chrome, open YouTube, and search CCTV"]
COMPLEX_PROMPT = "Design a website like YouTube for me"
DIRECT_REAL_CONDITIONS = {"Open Chrome": "Chrome is open", "Open Chrome and open YouTube": "youtube.com is loaded", "Open Chrome, open YouTube, and search CCTV": "CCTV query and visible results are present"}
DIRECT_ROUTE = ["inline-current-model", "chrome:control-chrome"]
COMPLEX_ROUTE = ["inline-current-model", "build-web-apps:frontend-app-builder"]
ADMITTED_ROUTE = ["task-analyze-skill", "workflow-skill", "build-web-apps:frontend-app-builder"]
ENDING_CHECKS = ["responsive", "console", "navigation", "accessibility", "visual"]
SCENARIO_ALLOWED_KEYS = {"prompt", "complexity", "route_type", "skill", "execution_surface", "route", "ending_real_condition", "timing_evidence"}
PSEUDO_ROUTE_IDS = {"inline-current-model"}
FIRST_RESULT_TIMING_EVIDENCE = "wall_clock_to_first_result_excluding_ending"


def materialize_dispatcher_plan(plan_template, cache_dir, entry_model, entry_effort):
    plan = deepcopy(plan_template)
    plan["cache_dir"] = str(Path(cache_dir).expanduser().resolve())
    plan["entry"] = {"model": entry_model, "effort": entry_effort}
    return plan


def admitted_dispatcher_template(path=FIXTURE_PATH):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload["admitted_dispatcher_template"]


def required_skill_ids(container):
    skill_ids = {container.get("skill")}
    skill_ids.update(container.get("route", []))
    plan = container.get("dispatcher_plan")
    if isinstance(plan, dict):
        skill_ids.update(node.get("skill") for node in plan.get("nodes", []) if isinstance(node, dict))
    return sorted(skill_id for skill_id in skill_ids if isinstance(skill_id, str) and skill_id and skill_id not in PSEUDO_ROUTE_IDS)


def _check_installed(skill_id, skills_root, failures):
    try:
        from skill_resolver import resolve_skill_path
    except ImportError:
        resolver_path = Path(__file__).with_name("skill_resolver.py")
        resolver_spec = importlib.util.spec_from_file_location("graduated_route_skill_resolver", resolver_path)
        resolver_module = importlib.util.module_from_spec(resolver_spec)
        resolver_spec.loader.exec_module(resolver_module)
        resolve_skill_path = resolver_module.resolve_skill_path
    if resolve_skill_path(skill_id, skills_root) is None:
        failures.append(f"skill is not installed: {skill_id}")


def _dispatcher_module():
    dispatcher_path = Path(__file__).with_name("task_route_dispatcher.py")
    dispatcher_spec = importlib.util.spec_from_file_location("graduated_route_dispatcher", dispatcher_path)
    dispatcher = importlib.util.module_from_spec(dispatcher_spec)
    dispatcher_spec.loader.exec_module(dispatcher)
    return dispatcher


def _validate_dispatcher_template(template, skills_root, failures):
    if not isinstance(template, dict):
        failures.append("admitted_dispatcher_template must be an object")
        return
    dispatcher = _dispatcher_module()
    supported_pairs = set(dispatcher.normal_adaptive_pair_texts())
    required_pairs = {"design": dispatcher.MODEL_ROLE_PAIRS["frontier_complex"], "implementation": dispatcher.MODEL_ROLE_PAIRS["balanced_complex"], "ending_real": dispatcher.MODEL_ROLE_PAIRS["balanced_complex"]}
    if template.get("activation") != "explicit_and_performance_admitted" or template.get("admission_precondition") != "positive_end_to_end_evidence_required":
        failures.append("admitted dispatcher template lacks explicit positive performance-admission precondition")
    if template.get("authorization") != "topology_only_not_execution_proof":
        failures.append("admitted dispatcher template must not claim execution authorization")
    if template.get("route") != ADMITTED_ROUTE:
        failures.append(f"admitted dispatcher route ordering must be {ADMITTED_ROUTE}")
    if template.get("illustrative_cold_start_pairs") != required_pairs or any(pair not in supported_pairs for pair in template.get("illustrative_cold_start_pairs", {}).values()):
        failures.append("admitted dispatcher static model/effort roles are incorrect or unsupported")
    if template.get("adaptive_result_producer") != "implementation":
        failures.append("admitted dispatcher adaptive producer must be implementation")
    if template.get("controller_transitions") != {"main_result_release": "observed_entry_coordinator", "ending_dispatch": "observed_entry_coordinator"}:
        failures.append("admitted dispatcher controller transitions are incorrect")
    if template.get("ending_checks") != ENDING_CHECKS:
        failures.append("admitted dispatcher Ending checks are incomplete")
    plan = template.get("dispatcher_plan")
    if not isinstance(plan, dict):
        failures.append("admitted dispatcher template dispatcher_plan must be an object")
        return
    expected_nodes = ["design", "implementation", "ending-real"]
    design_model, design_effort = required_pairs["design"].split("|", 1)
    implementation_model, implementation_effort = required_pairs["implementation"].split("|", 1)
    ending_model, ending_effort = required_pairs["ending_real"].split("|", 1)
    expected_roles = {"design": ("result", "build-web-apps:frontend-app-builder", design_model, design_effort, [], "general"), "implementation": ("result", "build-web-apps:frontend-app-builder", implementation_model, implementation_effort, ["design"], "general"), "ending-real": ("ending", "verify-skill", ending_model, ending_effort, ["implementation"], "general")}
    template_nodes = plan.get("nodes", [])
    if [node.get("id") for node in template_nodes if isinstance(node, dict)] != expected_nodes:
        failures.append("admitted dispatcher plan node topology is incorrect")
        return
    if plan.get("schema_version") != 2 or plan.get("main_result_node") != "implementation" or "mini_verify_node" in plan:
        failures.append("admitted dispatcher plan main/result-first contract is incorrect")
    if {node.get("phase") for node in template_nodes} != {"result", "ending"}:
        failures.append("admitted dispatcher plan must contain only result and ending phases")
    for node in template_nodes:
        observed = (node.get("phase"), node.get("skill"), node.get("model"), node.get("effort"), node.get("dependencies"), node.get("execution_domain"))
        if observed != expected_roles[node["id"]]:
            failures.append(f"admitted dispatcher plan role is incorrect for {node['id']}")
        pair_key = {"design": "design", "implementation": "implementation", "ending-real": "ending_real"}[node["id"]]
        if f"{node.get('model')}|{node.get('effort')}" != template["illustrative_cold_start_pairs"].get(pair_key):
            failures.append(f"admitted dispatcher plan pair is incorrect for {node['id']}")
    implementation = next(node for node in template_nodes if node.get("id") == "implementation")
    expected_ladder = dispatcher.normal_adaptive_pair_texts()
    if implementation.get("candidate_ladder") != expected_ladder or implementation.get("hard_floor") != dispatcher.MODEL_ROLE_PAIRS["floor"]:
        failures.append("admitted implementation must use the full catalog quality ladder with its role floor")
    recommendation = implementation.get("routing_recommendation", {})
    if recommendation.get("selected_pair") != required_pairs["implementation"] or recommendation.get("trial") is not False or not recommendation.get("profile_fingerprint"):
        failures.append("admitted implementation recommendation proof is invalid")
    with tempfile.TemporaryDirectory(prefix="graduated-admitted-plan-") as temporary:
        for model, efforts in dispatcher.ACTIVE_MODEL_EFFORTS.items():
            for effort in efforts:
                materialized = materialize_dispatcher_plan(plan, Path(temporary) / f"{model}-{effort}", model, effort)
                plan_failures = dispatcher.validate_plan(materialized, model, effort, Path(temporary), skills_root)
                failures.extend([f"admitted dispatcher plan {model}|{effort}: {failure}" for failure in plan_failures])
                for template_node, materialized_node in zip(template_nodes, materialized["nodes"]):
                    if (template_node.get("model"), template_node.get("effort")) != (materialized_node.get("model"), materialized_node.get("effort")):
                        failures.append(f"admitted dispatcher downstream pair inherited entry pair for {materialized_node['id']}")


def validate_fixture(path=FIXTURE_PATH, skills_root=None, require_installed=False):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"graduated fixture cannot be read: {error}"]
    failures = []
    scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("schema_version") != 2 or not isinstance(scenarios, list) or len(scenarios) != 4:
        return ["graduated fixture must contain schema 2 and exactly four scenarios"]
    by_prompt = {scenario.get("prompt"): scenario for scenario in scenarios if isinstance(scenario, dict)}
    if set(by_prompt) != set(DIRECT_PROMPTS + [COMPLEX_PROMPT]):
        failures.append("graduated fixture raw prompts do not match the required set")
    template = payload.get("admitted_dispatcher_template")
    resolved_skills_root = Path(skills_root).resolve() if skills_root is not None else _dispatcher_module().resolve_skills_root()
    if require_installed:
        for container in [*scenarios, template if isinstance(template, dict) else {}]:
            for skill_id in required_skill_ids(container):
                _check_installed(skill_id, resolved_skills_root, failures)
    for prompt in DIRECT_PROMPTS:
        scenario = by_prompt.get(prompt, {})
        if scenario.get("complexity") != "easy" or scenario.get("route_type") != "inline_tool" or scenario.get("skill") != "chrome:control-chrome":
            failures.append(f"{prompt}: must be an easy inline_tool route")
        if scenario.get("execution_surface") != "current_model_inline" or scenario.get("route") != DIRECT_ROUTE:
            failures.append(f"{prompt}: must stay on {DIRECT_ROUTE}")
        if scenario.get("ending_real_condition") != DIRECT_REAL_CONDITIONS[prompt]:
            failures.append(f"{prompt}: Ending Real condition is incorrect")
        if scenario.get("timing_evidence") != FIRST_RESULT_TIMING_EVIDENCE:
            failures.append(f"{prompt}: timing evidence must exclude post-result Ending work")
        if set(scenario) - SCENARIO_ALLOWED_KEYS:
            failures.append(f"{prompt}: inline route leaks dispatch or model execution")
    scenario = by_prompt.get(COMPLEX_PROMPT, {})
    if scenario.get("complexity") != "complex" or scenario.get("route_type") != "inline_production" or scenario.get("skill") != "build-web-apps:frontend-app-builder":
        failures.append("website scenario must be complex inline_production with canonical frontend skill")
    if scenario.get("execution_surface") != "current_model_inline" or scenario.get("route") != COMPLEX_ROUTE:
        failures.append(f"website scenario must stay on {COMPLEX_ROUTE}")
    if scenario.get("ending_real_condition") != "A rendered draft exists and core interaction paths render":
        failures.append("website scenario Ending Real condition is incorrect")
    if scenario.get("timing_evidence") != FIRST_RESULT_TIMING_EVIDENCE:
        failures.append("website scenario timing evidence must exclude post-result Ending work")
    if set(scenario) - SCENARIO_ALLOWED_KEYS:
        failures.append("website inline scenario leaks dispatch or model execution")
    for scenario in scenarios:
        if any(route_id in {"task-analyze-skill", "workflow-skill"} for route_id in scenario.get("route", [])):
            failures.append(f"{scenario.get('prompt')}: ordinary route invokes full routing skills")
    _validate_dispatcher_template(template, resolved_skills_root, failures)
    return failures


def main():
    parser = argparse.ArgumentParser(description="Validate graduated raw-prompt inline routes and admitted plan separation.")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    failures = validate_fixture(args.fixture, args.skills_root, args.require_installed)
    print(f"graduated-route-fixtures: {0 if failures else 4}/4 passed")
    if failures:
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
