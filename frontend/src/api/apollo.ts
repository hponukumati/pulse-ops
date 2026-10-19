/**
 * One Apollo client per authenticated session.
 *
 * The cache is in memory only and is discarded (stop + clearStore + socket dispose) on
 * logout or identity change, so no previous user's data can render. GraphQL requests
 * never send cookies (`credentials: "omit"`): they authenticate with the bearer access
 * token only. WebSocket auth uses `connection_init`, never the URL.
 */
import { ApolloClient, ApolloLink, HttpLink, InMemoryCache, split } from "@apollo/client";
import { ServerError } from "@apollo/client/errors";
import { ErrorLink } from "@apollo/client/link/error";
import { GraphQLWsLink } from "@apollo/client/link/subscriptions";
import { getMainDefinition } from "@apollo/client/utilities";
import { createClient, type Client } from "graphql-ws";
import { from, switchMap } from "rxjs";

import { freshToken, refresh } from "../auth/session";

/** Fired when the subscription socket (re)connects so views can refetch authoritative state. */
export const connectionEvents = new EventTarget();

export type SessionClient = { client: ApolloClient; dispose: () => Promise<void>; ws: Client };

function wsUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/graphql`;
}

export function createSessionClient(onAuthLost: () => void): SessionClient {
  const http = new HttpLink({ uri: "/graphql", credentials: "omit" });

  const auth = new ApolloLink((operation, forward) =>
    from(freshToken()).pipe(
      switchMap((token) => {
        operation.setContext(({ headers = {} }: { headers?: Record<string, string> }) => ({
          headers: token ? { ...headers, authorization: `Bearer ${token}` } : headers,
        }));
        return forward(operation);
      }),
    ),
  );

  const retryOnExpiry = new ErrorLink(({ error, operation, forward }) => {
    if (!ServerError.is(error) || error.statusCode !== 401 || operation.getContext().retriedAuth) return;
    operation.setContext({ retriedAuth: true });
    return from(refresh()).pipe(
      switchMap((next) => {
        if (!next) {
          onAuthLost();
          throw error;
        }
        return forward(operation);
      }),
    );
  });

  const ws = createClient({
    url: wsUrl(),
    lazy: true,
    retryAttempts: 20,
    connectionParams: async () => {
      const token = await freshToken();
      if (!token) {
        onAuthLost();
        throw new Error("login required");
      }
      return { authorization: `Bearer ${token}` };
    },
    on: {
      connected: () => connectionEvents.dispatchEvent(new Event("connected")),
      closed: (event) => {
        // 4403: session/grant revoked server-side. Re-check the session before retrying.
        if ((event as CloseEvent | undefined)?.code === 4403) void refresh().then((s) => s || onAuthLost(), () => undefined);
      },
    },
  });

  const transport = split(
    ({ query }) => {
      const def = getMainDefinition(query);
      return def.kind === "OperationDefinition" && def.operation === "subscription";
    },
    new GraphQLWsLink(ws),
    ApolloLink.from([retryOnExpiry, auth, http]),
  );

  const client = new ApolloClient({
    link: transport,
    cache: new InMemoryCache(),
    defaultOptions: { watchQuery: { fetchPolicy: "cache-and-network" } },
  });

  return {
    client,
    ws,
    dispose: async () => {
      await ws.dispose();
      client.stop();
      await client.clearStore();
    },
  };
}
