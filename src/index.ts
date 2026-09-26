import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const scriptPath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "scripts",
  "mail.py",
);

const secretInput = Type.Union(
  [
    Type.String({ minLength: 1 }),
    Type.Object(
      {
        source: Type.Union([
          Type.Literal("env"),
          Type.Literal("file"),
          Type.Literal("exec"),
          Type.Literal("store"),
        ]),
        provider: Type.String({ minLength: 1 }),
        id: Type.String({ minLength: 1 }),
      },
      { additionalProperties: false },
    ),
  ],
  { description: "IMAP password or SecretRef. Resolved to plaintext by OpenClaw before this plugin runs." },
);

const configSchema = Type.Object({
  host: Type.String({ description: "IMAP host, e.g. imap.mail.me.com" }),
  port: Type.Optional(Type.Number({ description: "IMAP port, default 993" })),
  user: Type.String({ description: "Mailbox login (full email address)" }),
  password: secretInput,
  mailbox: Type.Optional(Type.String({ description: "Default mailbox, default INBOX" })),
});

function runScript(config: any, args: string[]): string {
  if (typeof config.password !== "string") {
    throw new Error(
      "icloud-mail-manage: password did not resolve to a plaintext string (still a SecretRef object). Check configContracts.secretInputs in openclaw.plugin.json and the plugin config.",
    );
  }
  const fullArgs = [
    scriptPath,
    "--host",
    config.host,
    "--port",
    String(config.port ?? 993),
    "--user",
    config.user,
    ...args,
  ];
  return execFileSync("python3", fullArgs, {
    env: { ...process.env, ICLOUD_MAIL_PASSWORD: config.password },
    encoding: "utf8",
    timeout: 90000,
  });
}

function parseJsonLines(output: string): unknown[] {
  return output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return { raw: line };
      }
    });
}

export default defineToolPlugin({
  id: "icloud-mail-manage",
  name: "iCloud Mail Manage",
  description: "Search, sort (move), and mark read/unread on an IMAP mailbox. Never deletes mail.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "mail_list_folders",
      label: "List mail folders",
      description: "List all folders/mailboxes in the configured IMAP account.",
      parameters: Type.Object({}),
      execute: (_params, config) => {
        const out = runScript(config, ["list-folders"]);
        return { folders: out.split("\n").map((l) => l.trim()).filter(Boolean) };
      },
    }),
    tool({
      name: "mail_search",
      label: "Search mail",
      description: "Search the mailbox (read-only). Filter by unseen/seen, sender, subject, or date range.",
      parameters: Type.Object({
        mailbox: Type.Optional(Type.String({ description: "Mailbox to search, default INBOX" })),
        unseen: Type.Optional(Type.Boolean()),
        seen: Type.Optional(Type.Boolean()),
        from: Type.Optional(Type.String({ description: "Substring to match in the From header" })),
        subject: Type.Optional(Type.String({ description: "Substring to match in the Subject header" })),
        since: Type.Optional(Type.String({ description: "YYYY-MM-DD, messages on/after this date" })),
        before: Type.Optional(Type.String({ description: "YYYY-MM-DD, messages before this date" })),
        limit: Type.Optional(Type.Number({ description: "Max results, default 50" })),
      }),
      execute: (params, config) => {
        const args = ["--mailbox", params.mailbox ?? config.mailbox ?? "INBOX", "search"];
        if (params.unseen) args.push("--unseen");
        if (params.seen) args.push("--seen");
        if (params.from) args.push("--from", params.from);
        if (params.subject) args.push("--subject", params.subject);
        if (params.since) args.push("--since", params.since);
        if (params.before) args.push("--before", params.before);
        args.push("--limit", String(params.limit ?? 50));
        const out = runScript(config, args);
        return { messages: parseJsonLines(out) };
      },
    }),
    tool({
      name: "mail_mark",
      label: "Mark read/unread",
      description: "Mark a message as read (seen) or unread on the server.",
      parameters: Type.Object({
        mailbox: Type.Optional(Type.String()),
        uid: Type.String({ description: "Message UID from mail_search" }),
        seen: Type.Boolean({ description: "true = mark read, false = mark unread" }),
      }),
      execute: (params, config) => {
        const args = [
          "--mailbox",
          params.mailbox ?? config.mailbox ?? "INBOX",
          "mark",
          "--uid",
          params.uid,
          params.seen ? "--seen" : "--unseen",
        ];
        const out = runScript(config, args);
        return JSON.parse(out.trim());
      },
    }),
    tool({
      name: "mail_read",
      label: "Read mail content",
      description: "Fetch the text body of one message by UID. Read-only (uses BODY.PEEK, never marks it seen).",
      parameters: Type.Object({
        mailbox: Type.Optional(Type.String({ description: "Mailbox the message is in, default INBOX" })),
        uid: Type.String({ description: "Message UID from mail_search" }),
        maxChars: Type.Optional(Type.Number({ description: "Max body characters to return, default 4000" })),
      }),
      execute: (params, config) => {
        const args = [
          "--mailbox",
          params.mailbox ?? config.mailbox ?? "INBOX",
          "read",
          "--uid",
          params.uid,
          "--max-chars",
          String(params.maxChars ?? 4000),
        ];
        const out = runScript(config, args);
        return JSON.parse(out.trim());
      },
    }),
    tool({
      name: "mail_rename_folder",
      label: "Rename mail folder",
      description:
        "Rename an existing IMAP folder. Never rename INBOX, Sent Messages, Deleted Messages, Drafts, or Junk — those are special-use system folders other mail clients rely on by name.",
      parameters: Type.Object({
        name: Type.String({ description: "Current folder name (exact, from mail_list_folders)" }),
        to: Type.String({ description: "New folder name" }),
      }),
      execute: (params, config) => {
        const out = runScript(config, ["rename-folder", "--name", params.name, "--to", params.to]);
        return JSON.parse(out.trim());
      },
    }),
    tool({
      name: "mail_create_folder",
      label: "Create mail folder",
      description: "Create a new folder/mailbox on the IMAP account (e.g. for sorting important mail into).",
      parameters: Type.Object({
        name: Type.String({ description: "Folder name to create, e.g. Wichtig or Parent/Child" }),
      }),
      execute: (params, config) => {
        const out = runScript(config, ["create-folder", "--name", params.name]);
        return JSON.parse(out.trim());
      },
    }),
    tool({
      name: "mail_subscribe_folder",
      label: "Subscribe mail folder",
      description:
        "Subscribe an existing IMAP folder so mail clients (Apple Mail, iOS Mail) actually show and sync it. Folders created before this tool existed may need a one-time retroactive subscribe.",
      parameters: Type.Object({
        name: Type.String({ description: "Existing folder name, from mail_list_folders" }),
      }),
      execute: (params, config) => {
        const out = runScript(config, ["subscribe-folder", "--name", params.name]);
        return JSON.parse(out.trim());
      },
    }),
    tool({
      name: "mail_move",
      label: "Move/sort mail",
      description:
        "Move a message to another folder (IMAP UID MOVE). This is a sort operation, not a delete — the message is not removed from the account, only relocated.",
      parameters: Type.Object({
        mailbox: Type.Optional(Type.String({ description: "Source mailbox, default INBOX" })),
        uid: Type.String({ description: "Message UID from mail_search" }),
        to: Type.String({ description: "Destination folder name, from mail_list_folders" }),
      }),
      execute: (params, config) => {
        const args = [
          "--mailbox",
          params.mailbox ?? config.mailbox ?? "INBOX",
          "move",
          "--uid",
          params.uid,
          "--to",
          params.to,
        ];
        const out = runScript(config, args);
        return JSON.parse(out.trim());
      },
    }),
  ],
});
