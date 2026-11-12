# Grants app: demonstration only

This uses **synthetic data only** and a fictional policy. Any user authenticated in the configured tenant can enter the staff portal, see all applications, and make decisions. There is no staff-role check. Restrict the tenant, audience and network to trusted demo participants; this is not suitable for real citizen data or a production service.

## Configuration

Provision the infrastructure and permissions separately. Set these environment variables on the container app:

| Variable | Purpose |
|---|---|
| `TENANT_ID` | Allowed Microsoft Entra tenant ID, in UUID format. |
| `APP_ORIGIN` | Exact public HTTPS origin; used to check CSRF request origins.<br>(e.g. `https://demo.example.com`, without a trailing slash)|
| `PGHOST` | PostgreSQL server hostname. |
| `PGPORT` | PostgreSQL server port, defaults to `5432` |
| `PGDATABASE` | PostgreSQL database name. |
| `PGUSER` | PostgreSQL role for the managed identity. |
| `FOUNDRY_ENDPOINT` | Azure AI Foundry project endpoint. |
| `FOUNDRY_MODEL` | Deployed model name; the model must support structured output. |
| `UAMI_CLIENT_ID` | User-assigned managed identity client ID. |

The app obtains PostgreSQL and Foundry credentials using `ManagedIdentityCredential`, not passwords or API keys
- The managed identity must be:
    - Created as database principal and grant it database connect, schema creation and table permissions.
    - Granted the `Cognitive Services User` inference role for Foundry endpoint and `AcrPull` role for container image pull from ACR.
- PostgreSQL TLS uses certificate and host verification; add step in Dockerfile to install the server CA in the image trust store if required.

## Azure setup

1. Create a Container Apps environment integrated into a custom VNet. Configure Azure Files NFS, its private endpoint and private DNS; allow TCP 2049 from the environment. Register environment storage and mount it as `NfsAzureFile` at `/app`. Container Apps NFS does not support encryption in transit: configure the storage account accordingly, restrict the network, and protect the share from unauthorized writes. NFS mount access is a network/storage exception to token-based application service access.
2. Populate the share with `app.py`, `citizen.html`, `admin.html`, `policy.md`, `rubric.md` and the three `*_instructions.md` files. Protect these files: they include executable code and model instructions. Build and push the Docker image to ACR, attach the managed identity, configure its pull access, and run one replica with HTTPS ingress to port 8080. The Dockerfile's CMD runs the mounted app; its `/app` copy is not an automatic fallback.
3. Enable Container Apps built-in authentication with Microsoft Entra, require authentication for all routes, and configure the allowed tenant, audience and redirect URLs. Guests work only if configured in the tenant and registration. Ensure requests cannot bypass the platform authentication layer: the application trusts the platform-injected `X-MS-CLIENT-PRINCIPAL` header, so direct access or user-controlled spoofed headers would defeat authentication. Configure the platform session cookies as secure and HTTP-only. The app also checks tenant, origin and a CSRF token for writes.
4. Configure the environment variables above, verified-TLS network access to PostgreSQL and the Foundry project endpoint, and the managed identity roles. Restart the revision after changing mounted code, policy, rubric or instructions. The app creates its demo tables at startup; failures are reported rather than switching backends.

See the [Container Apps storage mounts](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts), [PostgreSQL managed identity](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/security-connect-with-managed-identity) and [Foundry managed identity](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/managed-identity) documentation for current platform setup details.
