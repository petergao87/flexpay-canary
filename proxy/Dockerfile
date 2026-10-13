FROM haproxy:3.2-alpine
USER 0
RUN addgroup -g 10002 control \
    && addgroup haproxy control \
    && mkdir -p /run/haproxy \
    && chown haproxy:control /run/haproxy \
    && chmod 2770 /run/haproxy
COPY proxy/haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg
USER 99:99
EXPOSE 8080 8404
CMD ["haproxy", "-W", "-db", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]
