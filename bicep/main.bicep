// Shared Azure resources for the green-screen agent demo: Log Analytics, Container Registry,
// a user-assigned identity with AcrPull, a virtual network, and a VNet-integrated Container Apps
// environment (external TCP ingress, used for TN3270 port 3270, requires a custom VNet).
// The container apps themselves are deployed by scripts\deploy-tk5.ps1 and
// scripts\deploy-green-screen-agent.ps1 into this environment.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short prefix for resource names.')
@minLength(2)
@maxLength(15)
param prefix string = 'gsa'

@description('Tags applied to every resource.')
param tags object = {
  app: 'green-screen-agent'
}

@description('Address space of the virtual network.')
param vnetAddressPrefix string = '10.42.0.0/16'

@description('Container Apps infrastructure subnet (workload profiles environments need at least /27).')
param infrastructureSubnetPrefix string = '10.42.0.0/23'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var registryName = take('${toLower(replace(prefix, '-', ''))}acr${uniqueString(resourceGroup().id)}', 50)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-identity'
  location: location
  tags: tags
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: 'container-apps'
        properties: {
          addressPrefix: infrastructureSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
    ]
  }
}

// The VNet configuration cannot be added to an existing environment; deploy-azure-resources.ps1
// recreates an environment that was created without one.
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: false
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output identityId string = identity.id
output environmentId string = environment.id
output environmentName string = environment.name
output environmentDefaultDomain string = environment.properties.defaultDomain
output infrastructureSubnetPrefix string = infrastructureSubnetPrefix
