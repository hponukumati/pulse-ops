/* eslint-disable */
import * as types from './graphql';
import type { TypedDocumentNode as DocumentNode } from '@graphql-typed-document-node/core';

/**
 * Map of all GraphQL operations in the project.
 *
 * This map has several performance disadvantages:
 * 1. It is not tree-shakeable, so it will include all operations in the project.
 * 2. It is not minifiable, so the string of a GraphQL query will be multiple times inside the bundle.
 * 3. It does not support dead code elimination, so it will add unused operations.
 *
 * Therefore it is highly recommended to use the babel or swc plugin for production.
 * Learn more about it here: https://the-guild.dev/graphql/codegen/plugins/presets/preset-client#reducing-bundle-size
 */
type Documents = {
    "\n  query Me {\n    me { id email role serviceIds reauthenticatedUntil }\n    systemStatus { evaluationStale evaluationsPaused lastWorkerHeartbeatAt }\n  }\n": typeof types.MeDocument,
    "\n  query Services {\n    services(first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          environment\n          canManage\n          health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n        }\n      }\n    }\n  }\n": typeof types.ServicesDocument,
    "\n  query Service($id: ID!) {\n    service(id: $id) {\n      id\n      name\n      environment\n      canManage\n      health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n    }\n  }\n": typeof types.ServiceDocument,
    "\n  query ServiceMetrics($serviceId: ID!, $range: MetricsRange!, $routeId: ID) {\n    metrics(serviceId: $serviceId, range: $range, routeId: $routeId) {\n      serviceId\n      range\n      routeId\n      asOf\n      windowStart\n      windowEnd\n      totalRequests\n      requestsPerMinute\n      errorRate5xx\n      clientErrorRate4xx\n      p50Ms\n      p95Ms\n      p99Ms\n      lastEventAt\n      sampleCount\n      status\n      timeseries { start end durationSeconds requests requestsPerMinute errorRate5xx p95Ms }\n      topEndpoints { routeId template method requests errorRate5xx clientErrorRate4xx p95Ms }\n    }\n  }\n": typeof types.ServiceMetricsDocument,
    "\n  query Routes($serviceId: ID!, $includeRetired: Boolean!) {\n    routes(serviceId: $serviceId, first: 100, includeRetired: $includeRetired) {\n      totalCount\n      edges { node { id template createdAt retiredAt } }\n    }\n  }\n": typeof types.RoutesDocument,
    "\n  query Incidents($serviceId: ID!, $activeOnly: Boolean!) {\n    incidents(serviceId: $serviceId, first: 50, activeOnly: $activeOnly) {\n      totalCount\n      edges {\n        node {\n          id\n          status\n          version\n          metric\n          threshold\n          openedValue\n          openedAt\n          acknowledgedAt\n          resolvedAt\n          resolveReason\n          rule { id name }\n        }\n      }\n    }\n  }\n": typeof types.IncidentsDocument,
    "\n  query AlertRules($serviceId: ID!) {\n    alertRules(serviceId: $serviceId, first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          metric\n          comparator\n          threshold\n          windowMinutes\n          minSamples\n          enabled\n          version\n          lastStatus\n          lastValue\n          lastSamples\n          lastEvaluatedSlot\n          breachStreak\n          okStreak\n        }\n      }\n    }\n  }\n": typeof types.AlertRulesDocument,
    "\n  query IngestTokens($id: ID!) {\n    service(id: $id) { id ingestTokens { id publicId createdAt expiresAt revokedAt } }\n  }\n": typeof types.IngestTokensDocument,
    "\n  subscription IncidentChanged($serviceId: ID!) {\n    incidentChanged(serviceId: $serviceId) { eventId version incident { id status version } }\n  }\n": typeof types.IncidentChangedDocument,
    "\n  mutation CreateService($input: ServiceInput!) { createService(input: $input) { id name } }\n": typeof types.CreateServiceDocument,
    "\n  mutation DeleteService($id: ID!) { deleteService(id: $id) }\n": typeof types.DeleteServiceDocument,
    "\n  mutation RegisterRoute($serviceId: ID!, $template: String!) { registerRoute(serviceId: $serviceId, template: $template) { id template } }\n": typeof types.RegisterRouteDocument,
    "\n  mutation RetireRoute($id: ID!) { retireRoute(id: $id) { id retiredAt } }\n": typeof types.RetireRouteDocument,
    "\n  mutation CreateIngestToken($serviceId: ID!) { createIngestToken(serviceId: $serviceId) { token { id publicId expiresAt } secret } }\n": typeof types.CreateIngestTokenDocument,
    "\n  mutation RotateIngestToken($id: ID!) { rotateIngestToken(id: $id) { token { id publicId expiresAt } secret } }\n": typeof types.RotateIngestTokenDocument,
    "\n  mutation RevokeIngestToken($id: ID!) { revokeIngestToken(id: $id) { id revokedAt } }\n": typeof types.RevokeIngestTokenDocument,
    "\n  mutation CreateAlertRule($serviceId: ID!, $input: AlertRuleInput!) { createAlertRule(serviceId: $serviceId, input: $input) { id } }\n": typeof types.CreateAlertRuleDocument,
    "\n  mutation UpdateAlertRule($id: ID!, $input: AlertRuleInput!) { updateAlertRule(id: $id, input: $input) { id version enabled } }\n": typeof types.UpdateAlertRuleDocument,
    "\n  mutation DeleteAlertRule($id: ID!) { deleteAlertRule(id: $id) { id } }\n": typeof types.DeleteAlertRuleDocument,
    "\n  mutation AcknowledgeIncident($id: ID!) { acknowledgeIncident(id: $id) { id status version } }\n": typeof types.AcknowledgeIncidentDocument,
    "\n  mutation ResolveIncident($id: ID!) { resolveIncident(id: $id) { id status version } }\n": typeof types.ResolveIncidentDocument,
    "\n  query Users {\n    users(first: 100) { totalCount edges { node { id email role disabled createdAt } } }\n    serviceGrants(first: 100) { edges { node { userId serviceId service { id name } } } }\n    services(first: 20) { edges { node { id name environment } } }\n  }\n": typeof types.UsersDocument,
    "\n  query AuditEvents($after: String) {\n    auditEvents(first: 50, after: $after) {\n      edges { node { id occurredAt actorKind actorId action targetType targetId result } }\n      pageInfo { hasNextPage endCursor }\n    }\n  }\n": typeof types.AuditEventsDocument,
    "\n  query Diagnostics {\n    diagnostics {\n      databaseOk\n      redisOk\n      lastWorkerHeartbeatAt\n      evaluationsPaused\n      pausedReason\n      outboxPending\n      outboxOldestPendingAt\n      lastDispatchOkAt\n      lastDispatchErrorAt\n      deletionJobsPending\n      retainedEvents\n      retainedEventLimit\n      eventRetentionDays\n    }\n  }\n": typeof types.DiagnosticsDocument,
    "\n  mutation SetUserRole($userId: ID!, $role: Role!) { setUserRole(userId: $userId, role: $role) { id role } }\n": typeof types.SetUserRoleDocument,
    "\n  mutation SetUserDisabled($userId: ID!, $disabled: Boolean!) { setUserDisabled(userId: $userId, disabled: $disabled) { id disabled } }\n": typeof types.SetUserDisabledDocument,
    "\n  mutation GrantServiceAccess($userId: ID!, $serviceId: ID!) { grantServiceAccess(userId: $userId, serviceId: $serviceId) { userId serviceId } }\n": typeof types.GrantServiceAccessDocument,
    "\n  mutation RevokeServiceAccess($userId: ID!, $serviceId: ID!) { revokeServiceAccess(userId: $userId, serviceId: $serviceId) }\n": typeof types.RevokeServiceAccessDocument,
    "\n  mutation SetEventRetentionDays($days: Int!) { setEventRetentionDays(days: $days) }\n": typeof types.SetEventRetentionDaysDocument,
};
const documents: Documents = {
    "\n  query Me {\n    me { id email role serviceIds reauthenticatedUntil }\n    systemStatus { evaluationStale evaluationsPaused lastWorkerHeartbeatAt }\n  }\n": types.MeDocument,
    "\n  query Services {\n    services(first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          environment\n          canManage\n          health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n        }\n      }\n    }\n  }\n": types.ServicesDocument,
    "\n  query Service($id: ID!) {\n    service(id: $id) {\n      id\n      name\n      environment\n      canManage\n      health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n    }\n  }\n": types.ServiceDocument,
    "\n  query ServiceMetrics($serviceId: ID!, $range: MetricsRange!, $routeId: ID) {\n    metrics(serviceId: $serviceId, range: $range, routeId: $routeId) {\n      serviceId\n      range\n      routeId\n      asOf\n      windowStart\n      windowEnd\n      totalRequests\n      requestsPerMinute\n      errorRate5xx\n      clientErrorRate4xx\n      p50Ms\n      p95Ms\n      p99Ms\n      lastEventAt\n      sampleCount\n      status\n      timeseries { start end durationSeconds requests requestsPerMinute errorRate5xx p95Ms }\n      topEndpoints { routeId template method requests errorRate5xx clientErrorRate4xx p95Ms }\n    }\n  }\n": types.ServiceMetricsDocument,
    "\n  query Routes($serviceId: ID!, $includeRetired: Boolean!) {\n    routes(serviceId: $serviceId, first: 100, includeRetired: $includeRetired) {\n      totalCount\n      edges { node { id template createdAt retiredAt } }\n    }\n  }\n": types.RoutesDocument,
    "\n  query Incidents($serviceId: ID!, $activeOnly: Boolean!) {\n    incidents(serviceId: $serviceId, first: 50, activeOnly: $activeOnly) {\n      totalCount\n      edges {\n        node {\n          id\n          status\n          version\n          metric\n          threshold\n          openedValue\n          openedAt\n          acknowledgedAt\n          resolvedAt\n          resolveReason\n          rule { id name }\n        }\n      }\n    }\n  }\n": types.IncidentsDocument,
    "\n  query AlertRules($serviceId: ID!) {\n    alertRules(serviceId: $serviceId, first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          metric\n          comparator\n          threshold\n          windowMinutes\n          minSamples\n          enabled\n          version\n          lastStatus\n          lastValue\n          lastSamples\n          lastEvaluatedSlot\n          breachStreak\n          okStreak\n        }\n      }\n    }\n  }\n": types.AlertRulesDocument,
    "\n  query IngestTokens($id: ID!) {\n    service(id: $id) { id ingestTokens { id publicId createdAt expiresAt revokedAt } }\n  }\n": types.IngestTokensDocument,
    "\n  subscription IncidentChanged($serviceId: ID!) {\n    incidentChanged(serviceId: $serviceId) { eventId version incident { id status version } }\n  }\n": types.IncidentChangedDocument,
    "\n  mutation CreateService($input: ServiceInput!) { createService(input: $input) { id name } }\n": types.CreateServiceDocument,
    "\n  mutation DeleteService($id: ID!) { deleteService(id: $id) }\n": types.DeleteServiceDocument,
    "\n  mutation RegisterRoute($serviceId: ID!, $template: String!) { registerRoute(serviceId: $serviceId, template: $template) { id template } }\n": types.RegisterRouteDocument,
    "\n  mutation RetireRoute($id: ID!) { retireRoute(id: $id) { id retiredAt } }\n": types.RetireRouteDocument,
    "\n  mutation CreateIngestToken($serviceId: ID!) { createIngestToken(serviceId: $serviceId) { token { id publicId expiresAt } secret } }\n": types.CreateIngestTokenDocument,
    "\n  mutation RotateIngestToken($id: ID!) { rotateIngestToken(id: $id) { token { id publicId expiresAt } secret } }\n": types.RotateIngestTokenDocument,
    "\n  mutation RevokeIngestToken($id: ID!) { revokeIngestToken(id: $id) { id revokedAt } }\n": types.RevokeIngestTokenDocument,
    "\n  mutation CreateAlertRule($serviceId: ID!, $input: AlertRuleInput!) { createAlertRule(serviceId: $serviceId, input: $input) { id } }\n": types.CreateAlertRuleDocument,
    "\n  mutation UpdateAlertRule($id: ID!, $input: AlertRuleInput!) { updateAlertRule(id: $id, input: $input) { id version enabled } }\n": types.UpdateAlertRuleDocument,
    "\n  mutation DeleteAlertRule($id: ID!) { deleteAlertRule(id: $id) { id } }\n": types.DeleteAlertRuleDocument,
    "\n  mutation AcknowledgeIncident($id: ID!) { acknowledgeIncident(id: $id) { id status version } }\n": types.AcknowledgeIncidentDocument,
    "\n  mutation ResolveIncident($id: ID!) { resolveIncident(id: $id) { id status version } }\n": types.ResolveIncidentDocument,
    "\n  query Users {\n    users(first: 100) { totalCount edges { node { id email role disabled createdAt } } }\n    serviceGrants(first: 100) { edges { node { userId serviceId service { id name } } } }\n    services(first: 20) { edges { node { id name environment } } }\n  }\n": types.UsersDocument,
    "\n  query AuditEvents($after: String) {\n    auditEvents(first: 50, after: $after) {\n      edges { node { id occurredAt actorKind actorId action targetType targetId result } }\n      pageInfo { hasNextPage endCursor }\n    }\n  }\n": types.AuditEventsDocument,
    "\n  query Diagnostics {\n    diagnostics {\n      databaseOk\n      redisOk\n      lastWorkerHeartbeatAt\n      evaluationsPaused\n      pausedReason\n      outboxPending\n      outboxOldestPendingAt\n      lastDispatchOkAt\n      lastDispatchErrorAt\n      deletionJobsPending\n      retainedEvents\n      retainedEventLimit\n      eventRetentionDays\n    }\n  }\n": types.DiagnosticsDocument,
    "\n  mutation SetUserRole($userId: ID!, $role: Role!) { setUserRole(userId: $userId, role: $role) { id role } }\n": types.SetUserRoleDocument,
    "\n  mutation SetUserDisabled($userId: ID!, $disabled: Boolean!) { setUserDisabled(userId: $userId, disabled: $disabled) { id disabled } }\n": types.SetUserDisabledDocument,
    "\n  mutation GrantServiceAccess($userId: ID!, $serviceId: ID!) { grantServiceAccess(userId: $userId, serviceId: $serviceId) { userId serviceId } }\n": types.GrantServiceAccessDocument,
    "\n  mutation RevokeServiceAccess($userId: ID!, $serviceId: ID!) { revokeServiceAccess(userId: $userId, serviceId: $serviceId) }\n": types.RevokeServiceAccessDocument,
    "\n  mutation SetEventRetentionDays($days: Int!) { setEventRetentionDays(days: $days) }\n": types.SetEventRetentionDaysDocument,
};

/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 *
 *
 * @example
 * ```ts
 * const query = graphql(`query GetUser($id: ID!) { user(id: $id) { name } }`);
 * ```
 *
 * The query argument is unknown!
 * Please regenerate the types.
 */
export function graphql(source: string): unknown;

/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Me {\n    me { id email role serviceIds reauthenticatedUntil }\n    systemStatus { evaluationStale evaluationsPaused lastWorkerHeartbeatAt }\n  }\n"): (typeof documents)["\n  query Me {\n    me { id email role serviceIds reauthenticatedUntil }\n    systemStatus { evaluationStale evaluationsPaused lastWorkerHeartbeatAt }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Services {\n    services(first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          environment\n          canManage\n          health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n        }\n      }\n    }\n  }\n"): (typeof documents)["\n  query Services {\n    services(first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          environment\n          canManage\n          health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n        }\n      }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Service($id: ID!) {\n    service(id: $id) {\n      id\n      name\n      environment\n      canManage\n      health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n    }\n  }\n"): (typeof documents)["\n  query Service($id: ID!) {\n    service(id: $id) {\n      id\n      name\n      environment\n      canManage\n      health { status sampleCount15m lastEventAt activeIncidents telemetryStale evaluationStale }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query ServiceMetrics($serviceId: ID!, $range: MetricsRange!, $routeId: ID) {\n    metrics(serviceId: $serviceId, range: $range, routeId: $routeId) {\n      serviceId\n      range\n      routeId\n      asOf\n      windowStart\n      windowEnd\n      totalRequests\n      requestsPerMinute\n      errorRate5xx\n      clientErrorRate4xx\n      p50Ms\n      p95Ms\n      p99Ms\n      lastEventAt\n      sampleCount\n      status\n      timeseries { start end durationSeconds requests requestsPerMinute errorRate5xx p95Ms }\n      topEndpoints { routeId template method requests errorRate5xx clientErrorRate4xx p95Ms }\n    }\n  }\n"): (typeof documents)["\n  query ServiceMetrics($serviceId: ID!, $range: MetricsRange!, $routeId: ID) {\n    metrics(serviceId: $serviceId, range: $range, routeId: $routeId) {\n      serviceId\n      range\n      routeId\n      asOf\n      windowStart\n      windowEnd\n      totalRequests\n      requestsPerMinute\n      errorRate5xx\n      clientErrorRate4xx\n      p50Ms\n      p95Ms\n      p99Ms\n      lastEventAt\n      sampleCount\n      status\n      timeseries { start end durationSeconds requests requestsPerMinute errorRate5xx p95Ms }\n      topEndpoints { routeId template method requests errorRate5xx clientErrorRate4xx p95Ms }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Routes($serviceId: ID!, $includeRetired: Boolean!) {\n    routes(serviceId: $serviceId, first: 100, includeRetired: $includeRetired) {\n      totalCount\n      edges { node { id template createdAt retiredAt } }\n    }\n  }\n"): (typeof documents)["\n  query Routes($serviceId: ID!, $includeRetired: Boolean!) {\n    routes(serviceId: $serviceId, first: 100, includeRetired: $includeRetired) {\n      totalCount\n      edges { node { id template createdAt retiredAt } }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Incidents($serviceId: ID!, $activeOnly: Boolean!) {\n    incidents(serviceId: $serviceId, first: 50, activeOnly: $activeOnly) {\n      totalCount\n      edges {\n        node {\n          id\n          status\n          version\n          metric\n          threshold\n          openedValue\n          openedAt\n          acknowledgedAt\n          resolvedAt\n          resolveReason\n          rule { id name }\n        }\n      }\n    }\n  }\n"): (typeof documents)["\n  query Incidents($serviceId: ID!, $activeOnly: Boolean!) {\n    incidents(serviceId: $serviceId, first: 50, activeOnly: $activeOnly) {\n      totalCount\n      edges {\n        node {\n          id\n          status\n          version\n          metric\n          threshold\n          openedValue\n          openedAt\n          acknowledgedAt\n          resolvedAt\n          resolveReason\n          rule { id name }\n        }\n      }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query AlertRules($serviceId: ID!) {\n    alertRules(serviceId: $serviceId, first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          metric\n          comparator\n          threshold\n          windowMinutes\n          minSamples\n          enabled\n          version\n          lastStatus\n          lastValue\n          lastSamples\n          lastEvaluatedSlot\n          breachStreak\n          okStreak\n        }\n      }\n    }\n  }\n"): (typeof documents)["\n  query AlertRules($serviceId: ID!) {\n    alertRules(serviceId: $serviceId, first: 20) {\n      totalCount\n      edges {\n        node {\n          id\n          name\n          metric\n          comparator\n          threshold\n          windowMinutes\n          minSamples\n          enabled\n          version\n          lastStatus\n          lastValue\n          lastSamples\n          lastEvaluatedSlot\n          breachStreak\n          okStreak\n        }\n      }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query IngestTokens($id: ID!) {\n    service(id: $id) { id ingestTokens { id publicId createdAt expiresAt revokedAt } }\n  }\n"): (typeof documents)["\n  query IngestTokens($id: ID!) {\n    service(id: $id) { id ingestTokens { id publicId createdAt expiresAt revokedAt } }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  subscription IncidentChanged($serviceId: ID!) {\n    incidentChanged(serviceId: $serviceId) { eventId version incident { id status version } }\n  }\n"): (typeof documents)["\n  subscription IncidentChanged($serviceId: ID!) {\n    incidentChanged(serviceId: $serviceId) { eventId version incident { id status version } }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CreateService($input: ServiceInput!) { createService(input: $input) { id name } }\n"): (typeof documents)["\n  mutation CreateService($input: ServiceInput!) { createService(input: $input) { id name } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation DeleteService($id: ID!) { deleteService(id: $id) }\n"): (typeof documents)["\n  mutation DeleteService($id: ID!) { deleteService(id: $id) }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation RegisterRoute($serviceId: ID!, $template: String!) { registerRoute(serviceId: $serviceId, template: $template) { id template } }\n"): (typeof documents)["\n  mutation RegisterRoute($serviceId: ID!, $template: String!) { registerRoute(serviceId: $serviceId, template: $template) { id template } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation RetireRoute($id: ID!) { retireRoute(id: $id) { id retiredAt } }\n"): (typeof documents)["\n  mutation RetireRoute($id: ID!) { retireRoute(id: $id) { id retiredAt } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CreateIngestToken($serviceId: ID!) { createIngestToken(serviceId: $serviceId) { token { id publicId expiresAt } secret } }\n"): (typeof documents)["\n  mutation CreateIngestToken($serviceId: ID!) { createIngestToken(serviceId: $serviceId) { token { id publicId expiresAt } secret } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation RotateIngestToken($id: ID!) { rotateIngestToken(id: $id) { token { id publicId expiresAt } secret } }\n"): (typeof documents)["\n  mutation RotateIngestToken($id: ID!) { rotateIngestToken(id: $id) { token { id publicId expiresAt } secret } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation RevokeIngestToken($id: ID!) { revokeIngestToken(id: $id) { id revokedAt } }\n"): (typeof documents)["\n  mutation RevokeIngestToken($id: ID!) { revokeIngestToken(id: $id) { id revokedAt } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CreateAlertRule($serviceId: ID!, $input: AlertRuleInput!) { createAlertRule(serviceId: $serviceId, input: $input) { id } }\n"): (typeof documents)["\n  mutation CreateAlertRule($serviceId: ID!, $input: AlertRuleInput!) { createAlertRule(serviceId: $serviceId, input: $input) { id } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation UpdateAlertRule($id: ID!, $input: AlertRuleInput!) { updateAlertRule(id: $id, input: $input) { id version enabled } }\n"): (typeof documents)["\n  mutation UpdateAlertRule($id: ID!, $input: AlertRuleInput!) { updateAlertRule(id: $id, input: $input) { id version enabled } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation DeleteAlertRule($id: ID!) { deleteAlertRule(id: $id) { id } }\n"): (typeof documents)["\n  mutation DeleteAlertRule($id: ID!) { deleteAlertRule(id: $id) { id } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation AcknowledgeIncident($id: ID!) { acknowledgeIncident(id: $id) { id status version } }\n"): (typeof documents)["\n  mutation AcknowledgeIncident($id: ID!) { acknowledgeIncident(id: $id) { id status version } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation ResolveIncident($id: ID!) { resolveIncident(id: $id) { id status version } }\n"): (typeof documents)["\n  mutation ResolveIncident($id: ID!) { resolveIncident(id: $id) { id status version } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Users {\n    users(first: 100) { totalCount edges { node { id email role disabled createdAt } } }\n    serviceGrants(first: 100) { edges { node { userId serviceId service { id name } } } }\n    services(first: 20) { edges { node { id name environment } } }\n  }\n"): (typeof documents)["\n  query Users {\n    users(first: 100) { totalCount edges { node { id email role disabled createdAt } } }\n    serviceGrants(first: 100) { edges { node { userId serviceId service { id name } } } }\n    services(first: 20) { edges { node { id name environment } } }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query AuditEvents($after: String) {\n    auditEvents(first: 50, after: $after) {\n      edges { node { id occurredAt actorKind actorId action targetType targetId result } }\n      pageInfo { hasNextPage endCursor }\n    }\n  }\n"): (typeof documents)["\n  query AuditEvents($after: String) {\n    auditEvents(first: 50, after: $after) {\n      edges { node { id occurredAt actorKind actorId action targetType targetId result } }\n      pageInfo { hasNextPage endCursor }\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Diagnostics {\n    diagnostics {\n      databaseOk\n      redisOk\n      lastWorkerHeartbeatAt\n      evaluationsPaused\n      pausedReason\n      outboxPending\n      outboxOldestPendingAt\n      lastDispatchOkAt\n      lastDispatchErrorAt\n      deletionJobsPending\n      retainedEvents\n      retainedEventLimit\n      eventRetentionDays\n    }\n  }\n"): (typeof documents)["\n  query Diagnostics {\n    diagnostics {\n      databaseOk\n      redisOk\n      lastWorkerHeartbeatAt\n      evaluationsPaused\n      pausedReason\n      outboxPending\n      outboxOldestPendingAt\n      lastDispatchOkAt\n      lastDispatchErrorAt\n      deletionJobsPending\n      retainedEvents\n      retainedEventLimit\n      eventRetentionDays\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation SetUserRole($userId: ID!, $role: Role!) { setUserRole(userId: $userId, role: $role) { id role } }\n"): (typeof documents)["\n  mutation SetUserRole($userId: ID!, $role: Role!) { setUserRole(userId: $userId, role: $role) { id role } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation SetUserDisabled($userId: ID!, $disabled: Boolean!) { setUserDisabled(userId: $userId, disabled: $disabled) { id disabled } }\n"): (typeof documents)["\n  mutation SetUserDisabled($userId: ID!, $disabled: Boolean!) { setUserDisabled(userId: $userId, disabled: $disabled) { id disabled } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation GrantServiceAccess($userId: ID!, $serviceId: ID!) { grantServiceAccess(userId: $userId, serviceId: $serviceId) { userId serviceId } }\n"): (typeof documents)["\n  mutation GrantServiceAccess($userId: ID!, $serviceId: ID!) { grantServiceAccess(userId: $userId, serviceId: $serviceId) { userId serviceId } }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation RevokeServiceAccess($userId: ID!, $serviceId: ID!) { revokeServiceAccess(userId: $userId, serviceId: $serviceId) }\n"): (typeof documents)["\n  mutation RevokeServiceAccess($userId: ID!, $serviceId: ID!) { revokeServiceAccess(userId: $userId, serviceId: $serviceId) }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation SetEventRetentionDays($days: Int!) { setEventRetentionDays(days: $days) }\n"): (typeof documents)["\n  mutation SetEventRetentionDays($days: Int!) { setEventRetentionDays(days: $days) }\n"];

export function graphql(source: string) {
  return (documents as any)[source] ?? {};
}

export type DocumentType<TDocumentNode extends DocumentNode<any, any>> = TDocumentNode extends DocumentNode<  infer TType,  any>  ? TType  : never;