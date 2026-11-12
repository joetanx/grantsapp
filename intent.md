# Grants App

## Business objective

Imagine a government agency in charge of welfare financial grants for less privileged citizens.

To handle the large volume of citizen applications for welfare request, the agency is designing an agentic application to alleviate manpower challenges.
- the agent is primarily an intake assistant and caseworker copilot - not an automated welfare decision-maker
- civil servants spend substantial time to explain eligibility criteria and application process, and clarify/request for information required for processing - agent can offload this from staff processing time
- application data tier keeps records of citizen requests history along with status (submitted, pending approval, approved, rejected, dropped)
- when an application has all required information, agent performs initial assessment according to policy rubric, and updates record status to "pending approval" along with its analysis
- staff will be the human-in-the-loop to approve or reject requests

## Outcome

A minimum viable product (MVP) and proof-of-concept pilot application for demonstration purposes only; the single priority is core functionality: it just needs to work.

- No need for production-grade engineering, complex architecture, or scalable design patterns.
- It is completely acceptable to bypass industry best practices for the sake of speed.
- Avoid over-engineering, skip writing comprehensive test suites, and do not worry about edge-case optimization or perfect UI styling.
- Focus exclusively on the shortest, simplest path to a functional, clickable demo.

# App design (raw ideas)

- deploy in azure container apps
- single python application file `app.py`, aiohttp listening on 8080, route: `/` is citizen facing Web UI, `/admin` is staff Web UI, `/api/*` for required backend actions
    - Web UI serves AJAX interface, actions route to respective `/api` endpoints
    - use dark-themed style sheet from bootstrap: `<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">`
    - the functions defined for `/api` can be reuse to exposed to tools for agent as required
- azure files nfs volume mount to container apps via virtual network + private endpoint
    - serves the app code, static html files, config, policy, instructions, assessment rubric files
    - app code would bake in image for production, but put in azure files for quick testing development - just update and restart the revision
- connect to azure postgres database for application data
- citizens and staff login to respective Web UI via entra sign in
    - the citizen/staff record should automatically populate respective table in postgres (some sort of SSO) upon first authenticated login, or match existing record
    - for simplicity, any sign in with valid entra login will be created as valid user of the respective portal
- LangGraph agent that can be interfaced through Web UI, or separate flows based on trigger (or certain api) to perform tasks
- application use managed identity for all required non-human access: Foundry model, container image pull from ACR, and all database operations from Web UI actions and agent workflows

