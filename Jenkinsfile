// Dedicated Linux staging agent; never connect this job to a production Docker host.
// Initialize its stable/proxy/monitoring baseline using the README before the first run.
pipeline {
    agent { label 'flexpay-staging-docker' }
    options {
        disableConcurrentBuilds()
        disableRestartFromStage()
        timestamps()
        timeout(time: 110, unit: 'MINUTES')
        skipDefaultCheckout(true)
    }
    parameters {
        booleanParam(name: 'DEPLOY_STAGING', defaultValue: true, description: 'Update only canary on the dedicated local staging Docker host.')
        string(name: 'STACK_NAME', defaultValue: 'flexpay-canary-staging', description: 'Pre-initialized staging Compose project name.')
        string(name: 'APPROVER', defaultValue: 'peter', description: 'Jenkins user ID or group allowed to approve tests and promotion.')
        string(name: 'DATABASE_FILE_CREDENTIAL', defaultValue: 'flexpay-staging-database-url', description: 'Jenkins Secret file credential ID.')
        string(name: 'PROVIDER_FILE_CREDENTIAL', defaultValue: 'flexpay-staging-provider-api-key', description: 'Jenkins Secret file credential ID.')
        string(name: 'GRAFANA_FILE_CREDENTIAL', defaultValue: 'flexpay-staging-grafana-password', description: 'Jenkins Secret file credential ID.')
    }
    environment {
        COMPOSE_PROJECT_NAME = "${params.STACK_NAME}"
        DOCKER_CONTEXT = 'default'
        PYTHONDONTWRITEBYTECODE = '1'
        STAGING_TOUCHED = 'false'
    }
    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    if (!(params.STACK_NAME ==~ /flexpay-canary-staging(?:-[a-z0-9-]+)?/)) {
                        error('STACK_NAME must be flexpay-canary-staging or a staging suffix.')
                    }
                    if (!params.APPROVER.trim()) {
                        error('An explicit APPROVER is required.')
                    }
                    env.CANARY_IMAGE = "flexpay/transaction-engine:build-${env.BUILD_NUMBER}-" +
                        sh(script: 'git rev-parse --short=12 HEAD', returnStdout: true).trim()
                }
            }
        }
        stage('Approve local validation') {
            steps {
                timeout(time: 20, unit: 'MINUTES') {
                    input message: 'Approve Dockerfile lint, mock unit/HTTP checks, Prometheus validation, image security scan, and staging mock load tests?',
                          ok: 'Approve validation', submitter: "${params.APPROVER}",
                          submitterParameter: 'VALIDATION_APPROVED_BY'
                }
            }
        }
        stage('Validate and build immutable image') {
            steps {
                sh '''
                    set -eu
                    endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
                    case "$endpoint" in
                        unix://*) ;;
                        *) echo 'Refusing a remote Docker endpoint; use the dedicated local staging agent.' >&2; exit 1 ;;
                    esac
                    mkdir -p .ci .cache/trivy
                    docker run --rm -i hadolint/hadolint:v2.15.1 < app/Dockerfile
                    docker run --rm -i hadolint/hadolint:v2.15.1 < proxy/Dockerfile
                    docker run --rm --entrypoint /bin/promtool \
                        -v "$WORKSPACE/monitoring/prometheus:/etc/prometheus:ro" \
                        prom/prometheus:v3.14.0 check config /etc/prometheus/prometheus.yml
                    docker run --rm --entrypoint /bin/promtool \
                        -v "$WORKSPACE/monitoring/prometheus:/etc/prometheus:ro" \
                        prom/prometheus:v3.14.0 test rules /etc/prometheus/rules.test.yml
                    docker build --pull -f app/Dockerfile -t "$CANARY_IMAGE" \
                        --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" .
                    docker image inspect --format '{{.Id}}' "$CANARY_IMAGE" > .ci/canary-image-id.txt
                    printf '%s\\n' "$CANARY_IMAGE" > .ci/canary-image-tag.txt
                '''
            }
        }
        stage('Mock unit and HTTP validation') {
            steps {
                sh '''
                    set -eu
                    # Copy only source/tests into disposable tmpfs; no payment secrets are mounted.
                    if docker run --rm --network none --read-only \
                        --cap-drop ALL --security-opt no-new-privileges:true \
                        --tmpfs /tmp:size=64m \
                        -v "$WORKSPACE/app:/source/app:ro" \
                        -v "$WORKSPACE/scripts:/source/scripts:ro" \
                        -v "$WORKSPACE/tests:/source/tests:ro" \
                        --entrypoint sh "$CANARY_IMAGE" -c \
                        'set -eu; mkdir -p /tmp/project; cp -R /source/app /source/scripts /source/tests /tmp/project/; cd /tmp/project; python -B -m unittest discover -s tests -v' \
                        > .ci/unit-tests.txt 2>&1; then
                        cat .ci/unit-tests.txt
                    else
                        cat .ci/unit-tests.txt
                        exit 1
                    fi
                '''
            }
        }
        stage('Security scan') {
            steps {
                sh '''
                    set -eu
                    # Scan an exported image instead of giving the scanner the Docker socket.
                    docker image save "$CANARY_IMAGE" -o .ci/canary-image.tar
                    if docker run --rm \
                        -v "$WORKSPACE/.ci:/input:ro" \
                        -v "$WORKSPACE/.cache/trivy:/cache" \
                        aquasec/trivy:0.75.0 image --input /input/canary-image.tar \
                        --cache-dir /cache --scanners vuln --severity HIGH,CRITICAL \
                        --ignore-unfixed --exit-code 1 --no-progress \
                        > .ci/trivy-report.txt 2>&1; then
                        cat .ci/trivy-report.txt
                    else
                        cat .ci/trivy-report.txt
                        exit 1
                    fi
                '''
            }
        }
        stage('Stage canary and preserve rollback image') {
            when { expression { params.DEPLOY_STAGING } }
            steps {
                withCredentials([
                    file(credentialsId: params.DATABASE_FILE_CREDENTIAL, variable: 'DATABASE_SECRET_SOURCE'),
                    file(credentialsId: params.PROVIDER_FILE_CREDENTIAL, variable: 'PROVIDER_SECRET_SOURCE'),
                    file(credentialsId: params.GRAFANA_FILE_CREDENTIAL, variable: 'GRAFANA_SECRET_SOURCE')
                ]) {
                    sh '''
                        set -eu
                        set +x
                        # Compose file secrets preserve source modes. The 0700 parent protects
                        # host access while 0444 files allow the non-root container to read them.
                        mkdir -p .secrets
                        chmod 0700 .secrets
                        install -m 0444 "$DATABASE_SECRET_SOURCE" .secrets/database_url
                        install -m 0444 "$PROVIDER_SECRET_SOURCE" .secrets/provider_api_key
                        install -m 0444 "$GRAFANA_SECRET_SOURCE" .secrets/grafana_admin_password
                    '''
                }
                script {
                    env.STABLE_IMAGE = sh(script: '''
                        set -eu
                        stable_id=$(docker compose ps -q stable)
                        proxy_id=$(docker compose ps -q proxy)
                        test -n "$stable_id" && test -n "$proxy_id"
                        test "$(docker inspect --format '{{.State.Running}}' "$stable_id")" = true
                        test "$(docker inspect --format '{{.State.Health.Status}}' "$stable_id")" = healthy
                        docker inspect --format '{{.Image}}' "$stable_id"
                    ''', returnStdout: true).trim()
                    writeFile file: '.ci/rollback-stable-image-id.txt', text: env.STABLE_IMAGE + '\n'
                    env.CANARY_IMAGE = readFile('.ci/canary-image-id.txt').trim()
                    // Failures after this point attempt a traffic-only rollback.
                    env.STAGING_TOUCHED = 'true'
                }
                sh '''
                    set -eu
                    docker compose config --quiet
                    # Restore the stable route before replacing a previous canary.
                    docker compose run --rm ops rollback
                    docker compose up -d --no-deps --no-build --pull never \
                        --wait --wait-timeout 90 canary
                    # HAProxy DNS/health checks can trail the container health check.
                    ready=false
                    for attempt in 1 2 3 4 5 6 7 8 9 10; do
                        if docker compose run --rm ops traffic 5; then
                            ready=true
                            break
                        fi
                        sleep 2
                    done
                    test "$ready" = true
                    docker run --rm --read-only --tmpfs /tmp:size=32m \
                        --cap-drop ALL --security-opt no-new-privileges:true \
                        --network "${COMPOSE_PROJECT_NAME}_control" \
                        -e FLEXPAY_INTEGRATION=1 -e FLEXPAY_BASE_URL=http://proxy:8080 \
                        -v "$WORKSPACE/tests:/workspace/tests:ro" -w /workspace \
                        --entrypoint python "$CANARY_IMAGE" \
                        -B -m unittest discover -s tests -p test_integration.py -v
                '''
            }
        }
        stage('Observe 5% then 10%') {
            when { expression { params.DEPLOY_STAGING } }
            steps {
                sh '''
                    set -eu
                    docker compose run --rm ops load --duration 240 --rps 30
                    docker compose run --rm ops analyze
                    docker compose run --rm ops traffic 10
                    docker compose run --rm ops load --duration 240 --rps 30
                    docker compose run --rm ops analyze
                    docker compose run --rm ops status
                '''
            }
        }
        stage('Approve promotion beyond 10%') {
            when { expression { params.DEPLOY_STAGING } }
            steps {
                timeout(time: 20, unit: 'MINUTES') {
                    input message: 'Review per-version errors, P95, sample size, target health and retained stable image. Approve progressive staging traffic 25% → 50% → 100%?',
                          ok: 'Approve promotion', submitter: "${params.APPROVER}",
                          submitterParameter: 'PROMOTION_APPROVED_BY'
                }
            }
        }
        stage('Progressive promotion') {
            when { expression { params.DEPLOY_STAGING } }
            steps {
                sh '''
                    set -eu
                    # An approval wait may expire the prior 5m sample window.
                    docker compose run --rm ops load --duration 240 --rps 30
                    docker compose run --rm ops analyze
                    # Each step rechecks readiness, sample size, errors and P95.
                    for percentage in 25 50 100; do
                        docker compose run --rm ops traffic "$percentage" --approve
                        docker compose run --rm ops load --duration 240 --rps 30
                        docker compose run --rm ops analyze
                    done
                    docker compose run --rm ops status > .ci/final-traffic-state.txt
                    stable_id=$(docker compose ps -q stable)
                    test "$(docker inspect --format '{{.Image}}' "$stable_id")" = "$STABLE_IMAGE"
                    cat .ci/final-traffic-state.txt
                    # Stable remains running at weight zero for immediate rollback.
                '''
            }
        }
    }
    post {
        unsuccessful {
            script {
                if (env.STAGING_TOUCHED == 'true') {
                    int rollbackStatus = sh(script: 'docker compose run --rm ops rollback', returnStatus: true)
                    if (rollbackStatus != 0) {
                        echo 'Rollback failed: page an operator and inspect stable/proxy availability immediately.'
                    }
                }
            }
        }
        always {
            archiveArtifacts artifacts: '.ci/*.txt', allowEmptyArchive: true, fingerprint: true
            sh 'if [ -f .ci/canary-image.tar ]; then rm -f .ci/canary-image.tar; fi'
            // Do not delete .secrets, Docker volumes, stable images, or the running stack.
        }
    }
}
