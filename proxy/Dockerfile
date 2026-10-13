FROM haproxy:3.2-alpine
USER root
RUN mkdir -p /run/haproxy && chown haproxy:haproxy /run/haproxy
COPY proxy/haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg
USER haproxy
EXPOSE 8080 8404
CMD ["haproxy", "-W", "-db", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]
