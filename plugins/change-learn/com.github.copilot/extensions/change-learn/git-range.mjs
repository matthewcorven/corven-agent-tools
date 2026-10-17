import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const gitLogFormat = "%H%x1f%h%x1f%ad%x1f%an%x1f%s%x1f%D%x1e";

async function run(file, args, options) {
    return execFileAsync(file, args, {
        cwd: options.cwd,
        encoding: "utf8",
        maxBuffer: 1024 * 1024,
        timeout: options.timeout ?? 10_000,
    });
}

export function parseCommits(output) {
    return output
        .split("\x1e")
        .map((record) => record.trim())
        .filter(Boolean)
        .map((record) => {
            const [id, shortId, date, author, subject, decorations = ""] = record.split("\x1f");
            return {
                id,
                shortId,
                date,
                author,
                subject,
                branches: decorations
                    .split(",")
                    .map((value) => value.trim())
                    .filter(Boolean),
                pullRequests: [],
            };
        })
        .filter((commit) => commit.id && commit.shortId);
}

function normalizePullRequests(pullRequests) {
    if (!Array.isArray(pullRequests)) {
        return [];
    }

    return pullRequests
        .filter((pullRequest) => Number.isInteger(pullRequest.number))
        .map((pullRequest) => ({
            number: pullRequest.number,
            title: typeof pullRequest.title === "string" ? pullRequest.title : "",
            headRefName: typeof pullRequest.headRefName === "string" ? pullRequest.headRefName : "",
            baseRefName: typeof pullRequest.baseRefName === "string" ? pullRequest.baseRefName : "",
            shas: [pullRequest.headRefOid, pullRequest.mergeCommit?.oid]
                .filter((sha) => typeof sha === "string" && sha.length > 0),
        }));
}

export function attachPullRequests(commits, pullRequests) {
    const bySha = new Map();
    for (const pullRequest of normalizePullRequests(pullRequests)) {
        for (const sha of pullRequest.shas) {
            const existing = bySha.get(sha) ?? [];
            existing.push({
                number: pullRequest.number,
                title: pullRequest.title,
                headRefName: pullRequest.headRefName,
                baseRefName: pullRequest.baseRefName,
            });
            bySha.set(sha, existing);
        }
    }

    return commits.map((commit) => ({
        ...commit,
        pullRequests: bySha.get(commit.id) ?? [],
    }));
}

export function commitLabel(commit) {
    const metadata = [
        ...commit.branches,
        ...commit.pullRequests.map((pullRequest) => {
            const target = pullRequest.baseRefName
                ? ` (${pullRequest.headRefName} -> ${pullRequest.baseRefName})`
                : "";
            const title = pullRequest.title ? `: ${pullRequest.title}` : "";
            return `PR #${pullRequest.number}${target}${title}`;
        }),
    ];
    const suffix = metadata.length ? ` [${metadata.join(" | ")}]` : "";
    return `${commit.shortId} - ${commit.subject}${suffix}`;
}

async function pullRequestsFor(repository) {
    try {
        const { stdout } = await run(
            "gh",
            [
                "pr",
                "list",
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,title,headRefName,baseRefName,headRefOid,mergeCommit",
            ],
            { cwd: repository, timeout: 3_000 },
        );
        return JSON.parse(stdout);
    } catch {
        return [];
    }
}

export async function loadCommitRange(repository, options = {}) {
    const { stdout: root } = await run(
        "git",
        ["rev-parse", "--show-toplevel"],
        { cwd: repository },
    );
    const workingDirectory = root.trim();
    const { stdout } = await run(
        "git",
        ["log", "--max-count", String(options.limit ?? 100), "--date=short", "--decorate=short", `--format=${gitLogFormat}`],
        { cwd: workingDirectory },
    );
    const commits = parseCommits(stdout);
    const pullRequests = options.includePullRequests === false
        ? []
        : await pullRequestsFor(workingDirectory);
    const enrichedCommits = attachPullRequests(commits, pullRequests);

    return {
        repository: workingDirectory,
        commits: enrichedCommits.map((commit) => ({
            ...commit,
            label: commitLabel(commit),
        })),
    };
}

export async function analyzeCommitRange({ repository, scriptPath, from, to, allowedCommitIds }) {
    if (!allowedCommitIds.has(from) || !allowedCommitIds.has(to)) {
        throw new Error("Select commits from the current repository range.");
    }
    if (from === to) {
        throw new Error("Choose different commits for the start and end of the range.");
    }

    try {
        await run("git", ["merge-base", "--is-ancestor", from, to], { cwd: repository });
    } catch {
        throw new Error("The From commit must be an ancestor of the To commit.");
    }

    const { stdout } = await run(
        "python3",
        [scriptPath, "--repo", repository, "summary", "--from", from, "--to", to],
        { cwd: repository, timeout: 30_000 },
    );
    try {
        return JSON.parse(stdout);
    } catch {
        throw new Error("Change Learn returned an invalid analysis result.");
    }
}
