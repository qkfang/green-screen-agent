using GreenScreenApp.Agents;

namespace GreenScreenApp.Api;

public record RunRequest(string? Message, string? PreviousResponseId);

public static class Api
{
    public static void MapAgentEndpoints(this WebApplication app, GreenScreenAgent agent, string? liveUiUrl, string? mcpUrl)
    {
        app.MapGet("/api/config", () => new
        {
            agent = agent.AgentName,
            version = agent.AgentVersion,
            mcpUrl,
            liveUiUrl
        });

        // Runs one natural-language action step. The Responses API keeps the conversation: send the
        // previous responseId to continue it.
        app.MapPost("/api/run", async (RunRequest request, ILogger<GreenScreenAgent> logger, CancellationToken ct) =>
        {
            if (string.IsNullOrWhiteSpace(request.Message))
            {
                return Results.BadRequest(new { error = "message is required." });
            }

            try
            {
                var result = await agent.RunStepAsync(request.Message.Trim(), request.PreviousResponseId, ct);
                return Results.Ok(result);
            }
            catch (OperationCanceledException)
            {
                return Results.StatusCode(StatusCodes.Status499ClientClosedRequest);
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "Agent step failed.");
                return Results.Json(new { error = ex.Message }, statusCode: StatusCodes.Status502BadGateway);
            }
        });
    }
}
