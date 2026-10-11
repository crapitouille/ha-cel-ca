// CEL-CA - storage of the Local Electricity Community on Azure.
// One Storage Account (Table = shared state, Queue = one inbox per HEMS).
// No VM, no broker, nothing to patch. Deploy with deploy.sh (Azure Cloud Shell).

targetScope = 'resourceGroup'

@description('Azure region (data stays in Switzerland by default).')
param location string = 'switzerlandnorth'

@description('Globally unique storage account name: 3-24 lower-case letters and digits.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Community identifier, used for tagging.')
param communityId string

param tags object = {
  application: 'cel-ca'
  community: communityId
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    // Members authenticate with SAS tokens signed by the account key.
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
  }
}

output account string = storage.name
output tableEndpoint string = storage.properties.primaryEndpoints.table
output queueEndpoint string = storage.properties.primaryEndpoints.queue
