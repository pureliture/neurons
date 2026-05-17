FROM gradle:9.5.1-jdk25-corretto AS build
WORKDIR /workspace
COPY settings.gradle build.gradle ./
COPY src ./src
COPY scripts ./scripts
RUN gradle bootJar --no-daemon

FROM amazoncorretto:25
WORKDIR /app
COPY --from=build /workspace/build/libs/*.jar /app/rag-ingress-queue.jar
ENTRYPOINT ["java", "-jar", "/app/rag-ingress-queue.jar"]
