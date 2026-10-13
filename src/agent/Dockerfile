FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY agent.csproj ./
RUN dotnet restore
COPY . ./
RUN dotnet publish agent.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app ./
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
USER $APP_UID
ENTRYPOINT ["dotnet", "green_screen_agent_app.dll"]
