pipeline {
    agent {
        kubernetes {
            label 'neurons-builder'
            defaultContainer 'builder'
            yaml """
apiVersion: v1
kind: Pod
metadata:
  labels:
    app: jenkins-agent
spec:
  nodeSelector:
    neurons.openclaw.io/local-registry: "true"
  containers:
  - name: builder
    image: gradle:9.5.1-jdk25-corretto
    command: ['sleep']
    args: ['infinity']
    resources:
      requests:
        cpu: "1"
        memory: "2Gi"
      limits:
        cpu: "2"
        memory: "4Gi"
  - name: docker
    image: docker:27-cli
    command: ['sleep']
    args: ['infinity']
    securityContext:
      runAsGroup: 973
    volumeMounts:
    - name: docker-sock
      mountPath: /var/run/docker.sock
  - name: git-tools
    image: alpine/git:latest
    command: ['sleep']
    args: ['infinity']
  volumes:
  - name: docker-sock
    hostPath:
      path: /var/run/docker.sock
"""
        }
    }

    environment {
        // k3s-master-01 로컬 레지스트리
        REGISTRY      = "localhost:5000"
        GITOPS_REPO   = "https://github.com/pureliture/neurons-ops.git"
        GITOPS_BRANCH = "main"
        GITOPS_ROOT   = "k3s/neurons/overlays/workload-canary-preview"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    // Kubernetes 임시 Agent에서는 checkout 컨테이너와 빌드 컨테이너의 UID/GID가 달라
                    // Git 2.35+ safe.directory 검증에 걸릴 수 있다. 현재 workspace만 신뢰 대상으로 등록한다.
                    sh 'git config --global --add safe.directory "$WORKSPACE"'

                    env.GIT_SHORT = sh(
                        script: 'git rev-parse --short HEAD',
                        returnStdout: true
                    ).trim()
                    env.IMAGE_TAG = "sha-${env.GIT_SHORT}"
                    echo "공통 이미지 태그: ${env.IMAGE_TAG}"
                }
            }
        }

        stage('Build & Test') {
            steps {
                container('builder') {
                    sh '''
                        echo "=== Gradle 빌드 & 테스트 ==="
                        gradle clean test bootJar --no-daemon --parallel
                    '''
                }
            }
            post {
                always {
                    junit allowEmptyResults: true,
                          testResults: '**/build/test-results/test/*.xml'
                }
            }
        }

        stage('Docker Build & Push All Neurons Images') {
            steps {
                container('docker') {
                    sh """
                        set -eu

                        echo "=== Java ingress-api 이미지 빌드 ==="
                        docker build \
                          -t ${env.REGISTRY}/neurons/ingress-api:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/ingress-api:latest \
                          .

                        echo "=== Python worker 공통 이미지 빌드 ==="
                        docker build -f worker/Dockerfile \
                          -t ${env.REGISTRY}/neurons/ingress-worker:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/ingress-worker:latest \
                          -t ${env.REGISTRY}/neurons/llm-brain-tools:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/llm-brain-tools:latest \
                          -t ${env.REGISTRY}/neurons/graph-trigger:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/graph-trigger:latest \
                          -t ${env.REGISTRY}/neurons/bulk-semantic-trigger:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/bulk-semantic-trigger:latest \
                          worker

                        echo "=== MCP HTTP 이미지 빌드 ==="
                        docker build -f worker/Dockerfile.mcp-http \
                          -t ${env.REGISTRY}/neurons/mcp-http:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/mcp-http:latest \
                          worker

                        echo "=== Session memory worker 이미지 빌드 ==="
                        docker build -f worker/Dockerfile.session-memory \
                          -t ${env.REGISTRY}/neurons/session-memory-worker:${env.IMAGE_TAG} \
                          -t ${env.REGISTRY}/neurons/session-memory-worker:latest \
                          worker

                        echo "=== 로컬 레지스트리 Push ==="
                        for image in \
                          neurons/ingress-api \
                          neurons/ingress-worker \
                          neurons/llm-brain-tools \
                          neurons/graph-trigger \
                          neurons/bulk-semantic-trigger \
                          neurons/mcp-http \
                          neurons/session-memory-worker
                        do
                          docker push ${env.REGISTRY}/\${image}:${env.IMAGE_TAG}
                          docker push ${env.REGISTRY}/\${image}:latest
                        done
                    """
                }
            }
        }

        stage('GitOps Update Preview Images') {
            steps {
                container('git-tools') {
                    withCredentials([usernamePassword(
                        credentialsId: 'github-pat',
                        usernameVariable: 'GIT_USER',
                        passwordVariable: 'GIT_TOKEN'
                    )]) {
                        sh """
                            set -eu

                            echo "=== neurons-ops 레포 클론 (${env.GITOPS_BRANCH}) ==="
                            rm -rf /tmp/neurons-ops
                            git clone --branch ${env.GITOPS_BRANCH} --single-branch https://\${GIT_USER}:\${GIT_TOKEN}@github.com/pureliture/neurons-ops.git /tmp/neurons-ops
                            cd /tmp/neurons-ops

                            git config user.email "jenkins@k3s-master-01"
                            git config user.name "Jenkins CI"

                            update_image() {
                              image="\$1"
                              file="\$2"
                              old="\$(grep -Eo "${env.REGISTRY}/\${image}:sha-[a-f0-9]+" "\$file" | head -1 || true)"
                              new="${env.REGISTRY}/\${image}:${env.IMAGE_TAG}"
                              echo "\$file: \${old:-<none>} -> \$new"
                              sed -i "s|${env.REGISTRY}/\${image}:sha-[a-f0-9]*|\$new|g" "\$file"
                            }

                            update_image "neurons/ingress-api" "${env.GITOPS_ROOT}/ingress-api.yaml"
                            update_image "neurons/ingress-worker" "${env.GITOPS_ROOT}/ingress-worker-health-only.yaml"
                            update_image "neurons/mcp-http" "${env.GITOPS_ROOT}/mcp-http.yaml"
                            update_image "neurons/graph-trigger" "${env.GITOPS_ROOT}/graph-workers-paused.yaml"
                            update_image "neurons/bulk-semantic-trigger" "${env.GITOPS_ROOT}/graph-workers-paused.yaml"

                            echo "=== 변경 확인 ==="
                            git diff -- \
                              "${env.GITOPS_ROOT}/ingress-api.yaml" \
                              "${env.GITOPS_ROOT}/ingress-worker-health-only.yaml" \
                              "${env.GITOPS_ROOT}/mcp-http.yaml" \
                              "${env.GITOPS_ROOT}/graph-workers-paused.yaml"

                            if git diff --quiet -- \
                              "${env.GITOPS_ROOT}/ingress-api.yaml" \
                              "${env.GITOPS_ROOT}/ingress-worker-health-only.yaml" \
                              "${env.GITOPS_ROOT}/mcp-http.yaml" \
                              "${env.GITOPS_ROOT}/graph-workers-paused.yaml"; then
                              echo "변경 없음 — GitOps push 생략"
                            else
                              echo "=== Git Push ==="
                              git add \
                                "${env.GITOPS_ROOT}/ingress-api.yaml" \
                                "${env.GITOPS_ROOT}/ingress-worker-health-only.yaml" \
                                "${env.GITOPS_ROOT}/mcp-http.yaml" \
                                "${env.GITOPS_ROOT}/graph-workers-paused.yaml"
                              git commit -m "ci: neurons preview 이미지 태그 업데이트

이미지 태그: ${env.IMAGE_TAG}
빌드: Jenkins #\${BUILD_NUMBER}
소스: ${env.GIT_SHORT}"
                              git push origin ${env.GITOPS_BRANCH}
                            fi
                        """
                    }
                }
            }
        }
    }

    post {
        success {
            echo """
            ✅ neurons multi-image CI 완료!
            태그: ${env.IMAGE_TAG}
            대상: ingress-api, ingress-worker, llm-brain-tools, graph-trigger,
                  bulk-semantic-trigger, mcp-http, session-memory-worker
            GitOps: workload-canary-preview 이미지 태그 업데이트
            확인: https://llm-brain-server.tailbf74be.ts.net:9443/
            """
        }
        failure {
            echo "❌ 파이프라인 실패 - 로그를 확인하세요"
        }
    }
}
