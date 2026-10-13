using Azure.AI.Extensions.OpenAI;
using Azure.AI.Projects;
using Azure.AI.Projects.Agents;
using OpenAI.Responses;

namespace GreenScreenApp.Agents;

/// <summary>One thing the agent did while handling a step, in the order it happened.</summary>
public record AgentEvent(string Kind, string Title, string? Detail);

public record AgentStepResult(string ResponseId, string Text, IReadOnlyList<AgentEvent> Events);

/// <summary>
/// A Foundry prompt agent. The agent version (model + instructions + tools) is created in the
/// Foundry project when the app starts; each step is a Responses API call against that agent.
/// MCP tools run inside Foundry, so no tool calls are executed here.
/// </summary>
public abstract class BaseAgent
{
    private readonly ProjectResponsesClient _responses;
    private readonly ILogger _logger;

    public string AgentName { get; }
    public string AgentVersion { get; }

    protected BaseAgent(AIProjectClient project, string agentName, string deploymentName, string instructions,
        IEnumerable<ResponseTool> tools, ILogger logger)
    {
        AgentName = agentName;
        _logger = logger;

        var definition = new DeclarativeAgentDefinition(model: deploymentName) { Instructions = instructions };
        foreach (var tool in tools)
        {
            definition.Tools.Add(tool);
        }

        var version = project.AgentAdministrationClient
            .CreateAgentVersion(agentName, new ProjectsAgentVersionCreationOptions(definition)).Value;
        AgentVersion = version.Version;
        _responses = project.ProjectOpenAIClient.GetProjectResponsesClientForAgent(version.Name);
        _logger.LogInformation("Prompt agent {Agent} version {Version} is ready ({Model}).", version.Name, version.Version, deploymentName);
    }

    /// <summary>Runs one step. Pass the previous response id to continue the same conversation.</summary>
    public async Task<AgentStepResult> RunStepAsync(string message, string? previousResponseId, CancellationToken ct = default)
    {
        var options = new CreateResponseOptions
        {
            PreviousResponseId = string.IsNullOrWhiteSpace(previousResponseId) ? null : previousResponseId,
            InputItems = { ResponseItem.CreateUserMessageItem(message) }
        };

        ResponseResult result = (await _responses.CreateResponseAsync(options, ct)).Value;
        var events = new List<AgentEvent>();

        // Tools are configured to never require approval; approvals would only stall the run, so decline them.
        while (result.OutputItems.OfType<McpToolCallApprovalRequestItem>().ToList() is { Count: > 0 } approvals)
        {
            var next = new CreateResponseOptions { PreviousResponseId = result.Id };
            foreach (var approval in approvals)
            {
                events.Add(new AgentEvent("error", $"Approval requested for {approval.ToolName}", "Tool approval is not supported; the call was declined."));
                next.InputItems.Add(ResponseItem.CreateMcpApprovalResponseItem(approval.Id, approved: false));
            }
            events.AddRange(ToEvents(result));
            result = (await _responses.CreateResponseAsync(next, ct)).Value;
        }

        events.AddRange(ToEvents(result));
        if (result.Error is not null)
        {
            events.Add(new AgentEvent("error", "The agent response failed", result.Error.Message));
        }
        return new AgentStepResult(result.Id, result.GetOutputText() ?? string.Empty, events);
    }

    private static IEnumerable<AgentEvent> ToEvents(ResponseResult result)
    {
        foreach (var item in result.OutputItems)
        {
            switch (item)
            {
                case McpToolCallItem call:
                    var detail = call.Error is not null
                        ? $"ERROR: {call.Error}"
                        : call.ToolOutput;
                    yield return new AgentEvent("mcp", $"{call.ToolName}({call.ToolArguments})", detail);
                    break;
                case McpToolCallApprovalRequestItem:
                    break;
            }
        }
    }
}
