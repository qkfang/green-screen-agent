using Azure.AI.Projects;
using Azure.Core;
using Azure.Identity;
using GreenScreenApp.Api;
using GreenScreenApp.Agents;

var builder = WebApplication.CreateBuilder(args);

builder.Logging.ClearProviders();
builder.Logging.AddConsole();

string Setting(string name, string? alias = null) =>
    builder.Configuration[name] ?? (alias is null ? null : builder.Configuration[alias])
    ?? throw new InvalidOperationException($"{name} is not set.");

var projectEndpoint = Setting("AZURE_AI_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT");
var deploymentName = Setting("AZURE_AI_MODEL_DEPLOYMENT_NAME", "FOUNDRY_MODEL_NAME");
var mcpUrl = new Uri(Setting("GREEN_SCREEN_MCP_URL"));
var liveUiUrl = builder.Configuration["GREEN_SCREEN_LIVE_URL"]
    ?? new Uri(mcpUrl, "/").ToString();

// In Azure, AZURE_CLIENT_ID selects the user-assigned managed identity; locally the app uses the `az login` account.
var clientId = builder.Configuration["AZURE_CLIENT_ID"];
TokenCredential credential = string.IsNullOrWhiteSpace(clientId)
    ? new AzureCliCredential(new AzureCliCredentialOptions { TenantId = builder.Configuration["AZURE_TENANT_ID"] })
    : new ManagedIdentityCredential(ManagedIdentityId.FromUserAssignedClientId(clientId));

builder.Services.AddHealthChecks();

var app = builder.Build();

app.UseDefaultFiles();
app.UseStaticFiles();
app.MapHealthChecks("/health");

// Creates (or updates) the prompt agent in the Foundry project, wired to the green-screen MCP server.
var project = new AIProjectClient(new Uri(projectEndpoint), credential);
var agent = new GreenScreenAgent(project, deploymentName, mcpUrl, app.Services.GetRequiredService<ILogger<GreenScreenAgent>>());

app.MapAgentEndpoints(agent, liveUiUrl, mcpUrl.ToString());

await app.RunAsync();
