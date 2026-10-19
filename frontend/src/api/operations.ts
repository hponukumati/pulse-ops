/** All GraphQL operations used by the UI (validated against docs/schema.graphql by codegen). */
import { graphql } from "../gql";

export const ME = graphql(`
  query Me {
    me { id email role serviceIds reauthenticatedUntil }
    systemStatus { evaluationStale evaluationsPaused lastWorkerHeartbeatAt }
  }
`);

export const SERVICES = graphql(`
  query Services {
    services(first: 20) {
      totalCount
      edges {
        node {
          id
          name
          environment
          canManage
          health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }
        }
      }
    }
  }
`);

export const SERVICE = graphql(`
  query Service($id: ID!) {
    service(id: $id) {
      id
      name
      environment
      canManage
      health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }
    }
  }
`);

export const METRICS = graphql(`
  query ServiceMetrics($serviceId: ID!, $range: MetricsRange!, $routeId: ID) {
    metrics(serviceId: $serviceId, range: $range, routeId: $routeId) {
      serviceId
      range
      routeId
      asOf
      windowStart
      windowEnd
      totalRequests
      requestsPerMinute
      errorRate5xx
      clientErrorRate4xx
      p50Ms
      p95Ms
      p99Ms
      lastEventAt
      sampleCount
      status
      timeseries { start end durationSeconds requests requestsPerMinute errorRate5xx p95Ms }
      topEndpoints { routeId template method requests errorRate5xx clientErrorRate4xx p95Ms }
    }
  }
`);

export const ROUTES = graphql(`
  query Routes($serviceId: ID!, $includeRetired: Boolean!) {
    routes(serviceId: $serviceId, first: 100, includeRetired: $includeRetired) {
      totalCount
      edges { node { id template createdAt retiredAt } }
    }
  }
`);

export const INCIDENTS = graphql(`
  query Incidents($serviceId: ID!, $activeOnly: Boolean!) {
    incidents(serviceId: $serviceId, first: 50, activeOnly: $activeOnly) {
      totalCount
      edges {
        node {
          id
          status
          version
          metric
          threshold
          openedValue
          openedAt
          acknowledgedAt
          resolvedAt
          resolveReason
          rule { id name }
        }
      }
    }
  }
`);

export const RULES = graphql(`
  query AlertRules($serviceId: ID!) {
    alertRules(serviceId: $serviceId, first: 20) {
      totalCount
      edges {
        node {
          id
          name
          metric
          comparator
          threshold
          windowMinutes
          minSamples
          enabled
          version
          lastStatus
          lastValue
          lastSamples
          lastEvaluatedSlot
          breachStreak
          okStreak
        }
      }
    }
  }
`);

export const TOKENS = graphql(`
  query IngestTokens($id: ID!) {
    service(id: $id) { id ingestTokens { id publicId createdAt expiresAt revokedAt } }
  }
`);

export const INCIDENT_CHANGED = graphql(`
  subscription IncidentChanged($serviceId: ID!) {
    incidentChanged(serviceId: $serviceId) { eventId version incident { id status version } }
  }
`);

export const CREATE_SERVICE = graphql(`
  mutation CreateService($input: ServiceInput!) { createService(input: $input) { id name } }
`);

export const DELETE_SERVICE = graphql(`
  mutation DeleteService($id: ID!) { deleteService(id: $id) }
`);

export const REGISTER_ROUTE = graphql(`
  mutation RegisterRoute($serviceId: ID!, $template: String!) { registerRoute(serviceId: $serviceId, template: $template) { id template } }
`);

export const RETIRE_ROUTE = graphql(`
  mutation RetireRoute($id: ID!) { retireRoute(id: $id) { id retiredAt } }
`);

export const CREATE_TOKEN = graphql(`
  mutation CreateIngestToken($serviceId: ID!) { createIngestToken(serviceId: $serviceId) { token { id publicId expiresAt } secret } }
`);

export const ROTATE_TOKEN = graphql(`
  mutation RotateIngestToken($id: ID!) { rotateIngestToken(id: $id) { token { id publicId expiresAt } secret } }
`);

export const REVOKE_TOKEN = graphql(`
  mutation RevokeIngestToken($id: ID!) { revokeIngestToken(id: $id) { id revokedAt } }
`);

export const CREATE_RULE = graphql(`
  mutation CreateAlertRule($serviceId: ID!, $input: AlertRuleInput!) { createAlertRule(serviceId: $serviceId, input: $input) { id } }
`);

export const UPDATE_RULE = graphql(`
  mutation UpdateAlertRule($id: ID!, $input: AlertRuleInput!) { updateAlertRule(id: $id, input: $input) { id version enabled } }
`);

export const DELETE_RULE = graphql(`
  mutation DeleteAlertRule($id: ID!) { deleteAlertRule(id: $id) { id } }
`);

export const ACK_INCIDENT = graphql(`
  mutation AcknowledgeIncident($id: ID!) { acknowledgeIncident(id: $id) { id status version } }
`);

export const RESOLVE_INCIDENT = graphql(`
  mutation ResolveIncident($id: ID!) { resolveIncident(id: $id) { id status version } }
`);

export const USERS = graphql(`
  query Users {
    users(first: 100) { totalCount edges { node { id email role disabled createdAt } } }
    serviceGrants(first: 100) { edges { node { userId serviceId service { id name } } } }
    services(first: 20) { edges { node { id name environment } } }
  }
`);

export const AUDIT = graphql(`
  query AuditEvents($after: String) {
    auditEvents(first: 50, after: $after) {
      edges { node { id occurredAt actorKind actorId action targetType targetId result } }
      pageInfo { hasNextPage endCursor }
    }
  }
`);

export const DIAGNOSTICS = graphql(`
  query Diagnostics {
    diagnostics {
      databaseOk
      redisOk
      lastWorkerHeartbeatAt
      evaluationsPaused
      pausedReason
      outboxPending
      outboxOldestPendingAt
      lastDispatchOkAt
      lastDispatchErrorAt
      deletionJobsPending
      retainedEvents
      retainedEventLimit
      eventRetentionDays
    }
  }
`);

export const SET_ROLE = graphql(`
  mutation SetUserRole($userId: ID!, $role: Role!) { setUserRole(userId: $userId, role: $role) { id role } }
`);

export const SET_DISABLED = graphql(`
  mutation SetUserDisabled($userId: ID!, $disabled: Boolean!) { setUserDisabled(userId: $userId, disabled: $disabled) { id disabled } }
`);

export const GRANT = graphql(`
  mutation GrantServiceAccess($userId: ID!, $serviceId: ID!) { grantServiceAccess(userId: $userId, serviceId: $serviceId) { userId serviceId } }
`);

export const REVOKE_GRANT = graphql(`
  mutation RevokeServiceAccess($userId: ID!, $serviceId: ID!) { revokeServiceAccess(userId: $userId, serviceId: $serviceId) }
`);

export const SET_RETENTION = graphql(`
  mutation SetEventRetentionDays($days: Int!) { setEventRetentionDays(days: $days) }
`);
