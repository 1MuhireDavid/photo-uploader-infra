# photo-uploader-infra

All infrastructure for the **Photo Uploader** lab: a highly available,
containerized fullstack photo gallery on **Amazon ECS Fargate**, in a
custom multi-AZ VPC, behind a public ALB, deployed via **CodePipeline +
CodeDeploy blue/green**, triggered automatically by container image
pushes. Images live in a **private S3 bucket** served exclusively through
**CloudFront** (Origin Access Control, no public bucket access); photo
descriptions live in **RDS PostgreSQL**. All infrastructure is
**CloudFormation**, deployed via **Git sync**; all CI/CD-to-AWS calls use
**OIDC** — no long-lived AWS credentials anywhere.

The application code lives in a separate repo:
[`photo-uploader-app`](https://github.com/1MuhireDavid/photo-uploader-app).

```
photo-uploader-infra/
├── README.md                 (this file)
├── bootstrap/00-bootstrap.yaml    # deployed ONCE, manually (see below)
├── cfn/
│   ├── root.yaml               # master template -- what Git sync deploys
│   ├── deployment-file.yaml     # Git sync's parameters/tags file
│   └── modules/
│       ├── 01-network.yaml         # VPC, public+private subnets x2 AZ, NAT
│       ├── 02-security.yaml         # least-privilege security groups
│       ├── 03-backing-services.yaml  # ECR repo + interface/gateway VPC endpoints
│       ├── 04-storage-cdn.yaml        # private S3 photos bucket + CloudFront (OAC)
│       ├── 05-database.yaml            # RDS PostgreSQL (photo metadata)
│       ├── 06-alb-ecs.yaml              # ALB, ECS cluster/service, autoscaling
│       └── 07-cicd-pipeline.yaml         # CodePipeline, CodeDeploy, EventBridge
├── diagram/architecture.drawio    # architecture diagram (draw.io / diagrams.net)
└── .github/workflows/package-templates.yml
```

## Architecture

- **Network:** 1 VPC, 2 AZs, 2 public + 2 private subnets, 1 NAT Gateway
  (parameterized to 2 for full HA egress).
- **Compute:** ECS Fargate tasks in **private** subnets only, no public
  IPs. Pulled images, shipped logs, STS calls, and the RDS-managed DB
  credential fetch all travel over **interface VPC endpoints** (`ecr.api`,
  `ecr.dkr`, `logs`, `sts`, `secretsmanager`) plus a free **S3 gateway
  endpoint** (ECR layer storage + the app's own photo uploads) — steady-
  state traffic never needs the NAT Gateway.
- **Exposure:** a public **ALB** is the only internet-facing compute
  resource; security groups form a strict chain (Internet → ALB SG → ECS
  task SG → {VPC endpoint SG on 443, RDS SG on 5432}), least privilege at
  every hop.
- **Images:** uploaded photos are stored in a **private** S3 bucket (all
  public access blocked) and served to browsers only through
  **CloudFront** using **Origin Access Control** — the bucket policy
  trusts nothing but this exact CloudFront distribution's ARN. The app
  writes to the bucket directly via its ECS task IAM role (no presigned
  URLs, no direct browser→S3 access, no CORS needed).
- **Metadata:** photo descriptions live in **RDS PostgreSQL**
  (`db.t3.micro`) in the same private subnets as ECS. The master
  credential is 100% RDS-managed (`ManageMasterUserPassword`) — it's
  generated and stored in Secrets Manager by AWS itself and injected into
  the container via the task definition's `Secrets` field; it never
  appears in any template, parameter, or repo.
- **Scaling:** target-tracking on `ECSServiceAverageCPUUtilization`,
  1 (min) / 1 (desired) / 4 (max) tasks.
- **Deploy:** ECS service uses `DeploymentController: CODE_DEPLOY`.
  EventBridge watches ECR for a `PUSH` of the `:latest` tag, starts
  CodePipeline, which hands the new image + `appspec.yaml`/`taskdef.json`
  (pulled from the **app repo**) to CodeDeploy for a blue/green traffic
  shift. The pipeline's GitHub source action has `DetectChanges: false`
  deliberately, so it never self-triggers on unrelated app-repo commits
  (e.g. a README edit) — EventBridge is the sole trigger.
- **IaC delivery:** CloudFormation **Git sync** deploys `cfn/root.yaml`
  straight from this repo on every push to `main`; nested stack templates
  are hosted in S3 (bucket created by a one-time bootstrap stack) since
  Git sync has no built-in `cfn package` step.
- **CI/CD auth:** this repo's workflow authenticates to AWS via **OIDC**
  (`sts:AssumeRoleWithWebIdentity`), scoped to this exact repo + workflow
  file; the app repo's build workflow does the same, independently scoped
  to itself. Zero stored AWS keys in either repo.

## Why two repos need two different auth mechanisms with AWS

| System | Direction | Auth mechanism |
|---|---|---|
| This repo's `package-templates.yml` | GitHub → AWS | **OIDC** federated role, scoped to this repo + workflow file |
| `photo-uploader-app`'s `build-and-push.yml` | GitHub → AWS | **OIDC** federated role, scoped to *that* repo + workflow file |
| CloudFormation Git sync | AWS → this repo (AWS reads this repo to deploy it) | AWS's native **CodeConnections** (GitHub App install) |
| CodePipeline's GitHub source action | AWS → `photo-uploader-app` (AWS reads that repo for `appspec.yaml`/`taskdef.json`) | The **same** CodeConnections connection, authorized against the app repo |

Both are secretless/keyless from GitHub's side; only the first two are
literally "OIDC" in the IAM sense, since OIDC federation only makes sense
for the direction where GitHub Actions is the caller.

## One-time bootstrap

`bootstrap/00-bootstrap.yaml` creates the S3 templates bucket, the
`token.actions.githubusercontent.com` OIDC provider (a **singleton per
AWS account** — leave `CreateOidcProvider` at `false` if any other lab in
this account already created one), and two scoped OIDC roles — one per
repo. Deployed once, manually, **not** through Git sync, because an OIDC
provider must never be at risk of being deleted/recreated by a routine
app or infra change.

**Deploy it via the AWS Console** (no CLI needed):

1. Sign in to the **AWS Console** and pick your target Region in the
   top-right region selector — everything else in this lab deploys into
   whatever region you pick here, so note it down.
2. *(Skip if you already know GitHub OIDC is set up in this account from
   another lab.)* Go to **IAM → Identity providers**. If
   `token.actions.githubusercontent.com` is already listed, keep
   `CreateOidcProvider` at `false` in step 5.
3. Go to **CloudFormation → Stacks → Create stack → With new resources
   (standard)**.
4. Under **Specify template**, choose **Upload a template file → Choose
   file**, and select `bootstrap/00-bootstrap.yaml` from your local
   clone. Click **Next**.
5. **Stack name:** `photo-uploader-bootstrap`. Fill in the parameters:

   | Parameter | Value |
   |---|---|
   | ProjectName | `photo-uploader` (default) |
   | GitHubOrg | `1MuhireDavid` |
   | InfraRepoName | `photo-uploader-infra` (default) |
   | AppRepoName | `photo-uploader-app` (default) |
   | AllowedGitRef | `refs/heads/main` (default) |
   | InfraPackagingWorkflowFile | leave default unless you renamed the workflow file |
   | AppBuildWorkflowFile | leave default unless you renamed the workflow file |
   | CreateOidcProvider | `false` unless step 2 found no existing provider |

   Click **Next**.
6. **Configure stack options:** leave everything at its default (add tags
   here if your account requires them). Click **Next**.
7. On the **Review** page, scroll to the **Capabilities** box at the
   bottom and check **"I acknowledge that AWS CloudFormation might create
   IAM resources with custom names."** This is required because the
   template creates two named IAM roles. Click **Submit**.
8. Wait for the stack status to reach **CREATE_COMPLETE** (S3 + IAM only,
   typically under two minutes).
9. Click into the stack and open its **Outputs** tab. You'll need three
   values from here: `TemplatesBucketName`, `InfraPackagingRoleArn`, and
   `AppEcrPushRoleArn`.

## Full setup order

1. **Deploy the bootstrap stack via the Console** — see above. Note the
   three Output values.
2. **Add this repo's secrets** (GitHub's UI): go to
   `github.com/1MuhireDavid/photo-uploader-infra` → **Settings → Secrets
   and variables → Actions**, and add:

   | Secret name | Value |
   |---|---|
   | `AWS_INFRA_PACKAGING_ROLE_ARN` | the `InfraPackagingRoleArn` output |
   | `AWS_TEMPLATES_BUCKET` | the `TemplatesBucketName` output |

   Then under **Variables**, add `AWS_REGION` = the region you deployed
   into (step 1).
3. **Add the app repo's secrets** — same Console flow, but on
   `github.com/1MuhireDavid/photo-uploader-app`:

   | Secret name | Value |
   |---|---|
   | `AWS_ECR_PUSH_ROLE_ARN` | the `AppEcrPushRoleArn` output |
   | `ECR_REPOSITORY` | `photo-uploader-app` |

   And `AWS_REGION` under **Variables**, same value as above.
4. **Fill in `cfn/deployment-file.yaml`** in this repo — replace
   `TemplatesBucketName`'s placeholder with the real output value, and set
   `AppOwnerName` to your full name. `GitHubOrg`/`AppRepoName` are already
   filled in. Commit the change.
5. **Push this repo to GitHub on `main`.** `.github/workflows/
   package-templates.yml` runs automatically and uploads the nested
   templates to S3. It does **not** touch the repo itself — open the run's
   job summary (or its "Print next manual step" log line) for the short
   SHA it uploaded under, then paste that into `cfn/deployment-file.yaml`'s
   `TemplatesVersion` parameter yourself and commit. That commit is what
   Git sync (once turned on, step 6) picks up to deploy.
6. **Turn on Git sync**, entirely in the CloudFormation console:
   - **CloudFormation → Stacks → Create stack → With Git sync**.
   - Connect to `1MuhireDavid/photo-uploader-infra`, branch `main`.
   - Deployment file path: `cfn/deployment-file.yaml`.
   - Accept the console's defaults for the Git sync service role and
     stack execution role, granting `CAPABILITY_NAMED_IAM`.
   - Git sync opens a pull request confirming the deployment file schema
     — merge it to kick off the first deploy.
   - Watch the stack's **Events** tab; the full nested-stack deploy (VPC,
     NAT, S3/CloudFront, RDS, ALB, ECS, pipeline) typically takes
     15–20 minutes (RDS is the slowest single resource).
7. **Authorize the GitHub connection** — in the CloudFormation console,
   go to **Developer Tools → Settings → Connections**, find the
   connection created by `07-cicd-pipeline.yaml` (status **Pending**),
   click it, and **Update pending connection** to complete the one-click
   GitHub App authorization against `photo-uploader-app`. The pipeline's
   GitHub source action won't run until this is **Available**.
8. **Push the app** — see `photo-uploader-app`'s README for filling in
   `ecs/taskdef.json` and triggering the first real image build. That
   push builds/pushes the image, EventBridge fires, CodePipeline runs,
   CodeDeploy shifts traffic blue → green.
9. **Open the app** — CloudFormation console → root stack
   (`photo-uploader`) → **Outputs** tab → `AlbEndpoint`. The
   `CloudFrontDomainName` output is where uploaded images are served from.

## Deliverables checklist

| Deliverable | Where |
|---|---|
| Infra CloudFormation | this repo |
| App code + Dockerfile + build/deploy files | [`photo-uploader-app`](https://github.com/1MuhireDavid/photo-uploader-app) |
| ALB endpoint | CloudFormation output `AlbEndpoint` on the root stack, after step 8 |
| Architecture diagram (draw.io) | `diagram/architecture.drawio` |

## Rubric → implementation map

| Rubric item | Implementation |
|---|---|
| Multi-AZ VPC, correct subnets | `cfn/modules/01-network.yaml` |
| Private ECS + VPC endpoints + public ALB | `03-backing-services.yaml`, `06-alb-ecs.yaml` |
| CloudFront + private S3 bucket restricted via OAC | `04-storage-cdn.yaml` |
| RDS PostgreSQL, db.t3 family, private subnets | `05-database.yaml` |
| Least-privilege security groups | `02-security.yaml` (strict SG-to-SG chain) |
| All resources via CFN + Git sync | `cfn/root.yaml` + `cfn/deployment-file.yaml` |
| GitHub Actions builds & pushes image | `photo-uploader-app/.github/workflows/build-and-push.yml` |
| OIDC auth (no long-lived secrets) | Both workflows use `role-to-assume`; roles + `job_workflow_ref` scoping in `bootstrap/00-bootstrap.yaml` |
| EventBridge triggers CodeDeploy on ECR push | `07-cicd-pipeline.yaml`: `EcrPushRule` |
| App accessible via ALB | `AlbEndpoint` output |
| ALB health checks pass | Health check path `/`, answered by both the bootstrap placeholder and the real app |
| CloudWatch Logs | `awslogs` driver → `/ecs/photo-uploader-app` log group |
| Auto scaling 1–4 on CPU | `ScalableTarget` / `CpuScalingPolicy` in `06-alb-ecs.yaml` |
| Blue/green deployment | `07-cicd-pipeline.yaml`: CodeDeploy `BLUE_GREEN` + `WITH_TRAFFIC_CONTROL`, two target groups |

## Security & cost notes

- Every security group is scoped to the single upstream SG that should
  reach it (Internet → ALB SG :80 → ECS task SG :container-port →
  {VPC endpoint SG :443, RDS SG :5432}) — no `0.0.0.0/0` ingress below the
  ALB.
- ECS tasks run in **private** subnets with `AssignPublicIp: DISABLED`;
  all outbound calls that matter (ECR, CloudWatch Logs, STS, Secrets
  Manager) go over interface VPC endpoints, and both ECR's image-layer
  store and the app's own photo uploads go over a free S3 gateway
  endpoint — NAT Gateway is present for other/edge-case internet egress
  but ordinary steady-state traffic never touches it.
- The photos S3 bucket blocks all public access; the only principal ever
  granted `s3:GetObject` is `cloudfront.amazonaws.com`, further scoped by
  `AWS:SourceArn` to this exact distribution.
- RDS is `PubliclyAccessible: false`, its own security group only accepts
  5432 from the ECS task security group, and its master credential is
  entirely AWS-managed (no password in any template/parameter/secret you
  create yourself).
- `SingleNatGateway=true` by default (1 NAT Gateway) to keep the lab
  cheap; flip to `false` for fully-HA egress in a production setting.
  `DbMultiAZ=false` by default for the same reason — the rubric requires
  a multi-AZ *VPC*, not a multi-AZ database.
- All S3 buckets: private, encrypted, versioned, deny-insecure-transport.
- IAM roles are purpose-scoped (e.g. the ECR-push role can only push to
  its own single ECR repository ARN; the ECS task role can only
  read/write the photos bucket; nothing has broader access than its one
  job requires).
- Every resource is tagged `Project`/`ManagedBy`/`Environment` (propagated
  from `deployment-file.yaml`'s `tags:` block through the nested stacks).

## Viewing / editing the diagram

`diagram/architecture.drawio` opens directly in
[diagrams.net](https://app.diagrams.net) (File → Open From → Device), the
[draw.io desktop app](https://github.com/jgraph/drawio-desktop), or the
[Draw.io Integration VS Code extension](https://marketplace.visualstudio.com/items?itemName=hediet.vscode-drawio).
To export a PNG/SVG for a slide or doc, open it in any of those and use
**File → Export as**.
