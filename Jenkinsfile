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
        // 로컬 레지스트리 (k3s-master-01 에서 접근)
        REGISTRY       = "localhost:5000"
        IMAGE_NAME     = "neurons/ingress-api"
        // neurons-ops GitOps 레포
        GITOPS_REPO    = "https://github.com/pureliture/neurons-ops.git"
        GITOPS_BRANCH  = "main"
        GITOPS_PATH    = "k3s/neurons/overlays/workload-canary-preview/ingress-api.yaml"
    }

    stages {
        // -------------------------------------------------------
        // Stage 1: 소스 체크아웃
        // -------------------------------------------------------
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    // Kubernetes 임시 Agent에서는 checkout 컨테이너와 빌드 컨테이너의 UID/GID가 달라
                    // Git 2.35+ safe.directory 검증에 걸릴 수 있다. 현재 workspace만 신뢰 대상으로 등록한다.
                    sh 'git config --global --add safe.directory "$WORKSPACE"'

                    // 짧은 커밋 해시 (이미지 태그로 사용)
                    env.GIT_SHORT = sh(
                        script: 'git rev-parse --short HEAD',
                        returnStdout: true
                    ).trim()
                    env.IMAGE_TAG  = "sha-${env.GIT_SHORT}"
                    env.IMAGE_FULL = "${env.REGISTRY}/${env.IMAGE_NAME}:${env.IMAGE_TAG}"
                    echo "이미지 태그: ${env.IMAGE_FULL}"
                }
            }
        }

        // -------------------------------------------------------
        // Stage 2: 빌드 & 테스트
        // -------------------------------------------------------
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
                    // 테스트 리포트 수집
                    junit allowEmptyResults: true,
                          testResults: '**/build/test-results/test/*.xml'
                }
            }
        }

        // -------------------------------------------------------
        // Stage 3: Docker 이미지 빌드 & 로컬 레지스트리 Push
        // -------------------------------------------------------
        stage('Docker Build & Push') {
            steps {
                container('docker') {
                    sh """
                        echo "=== Docker 이미지 빌드 ==="
                        docker build -t ${env.IMAGE_FULL} .

                        echo "=== 로컬 레지스트리 Push ==="
                        docker push ${env.IMAGE_FULL}

                        echo "=== latest 태그도 Push ==="
                        docker tag ${env.IMAGE_FULL} ${env.REGISTRY}/${env.IMAGE_NAME}:latest
                        docker push ${env.REGISTRY}/${env.IMAGE_NAME}:latest
                    """
                }
            }
        }

        // -------------------------------------------------------
        // Stage 4: GitOps 레포 이미지 태그 업데이트
        // neurons-ops 레포의 YAML 파일에서 이미지 태그를 새 버전으로 교체
        // ArgoCD가 변경 감지 → 자동 배포
        // -------------------------------------------------------
        stage('GitOps Update') {
            steps {
                container('git-tools') {
                    withCredentials([usernamePassword(
                        credentialsId: 'github-pat',
                        usernameVariable: 'GIT_USER',
                        passwordVariable: 'GIT_TOKEN'
                    )]) {
                        sh """
                            echo "=== neurons-ops 레포 클론 (${env.GITOPS_BRANCH}) ==="
                            rm -rf /tmp/neurons-ops
                            git clone --branch ${env.GITOPS_BRANCH} --single-branch https://\${GIT_USER}:\${GIT_TOKEN}@github.com/pureliture/neurons-ops.git /tmp/neurons-ops
                            cd /tmp/neurons-ops

                            git config user.email "jenkins@k3s-master-01"
                            git config user.name "Jenkins CI"

                            echo "=== 이미지 태그 업데이트 ==="
                            OLD_TAG=\$(grep -Eo 'sha-[a-f0-9]+' ${env.GITOPS_PATH} | head -1)
                            echo "이전 태그: \$OLD_TAG → 새 태그: ${env.IMAGE_TAG}"

                            sed -i "s|${env.REGISTRY}/${env.IMAGE_NAME}:sha-[a-f0-9]*|${env.IMAGE_FULL}|g" ${env.GITOPS_PATH}

                            echo "=== 변경 확인 ==="
                            git diff ${env.GITOPS_PATH}

                            echo "=== Git Push ==="
                            git add ${env.GITOPS_PATH}
                            git commit -m "ci: neurons-ingress-api 이미지 태그 업데이트

이미지: ${env.IMAGE_FULL}
빌드: Jenkins #\${BUILD_NUMBER}
소스: ${env.GIT_SHORT}"

                            git push origin ${env.GITOPS_BRANCH}
                        """
                    }
                }
            }
        }
    }

    // -------------------------------------------------------
    // 파이프라인 완료 후 알림
    // -------------------------------------------------------
    post {
        success {
            echo """
            ✅ 배포 완료!
            이미지: ${env.IMAGE_FULL}
            ArgoCD가 변경을 감지해 자동 배포합니다.
            확인: https://llm-brain-server.tailbf74be.ts.net:9443/
            """
        }
        failure {
            echo "❌ 파이프라인 실패 - 로그를 확인하세요"
        }
    }
}
