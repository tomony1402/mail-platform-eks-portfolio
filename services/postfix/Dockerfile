FROM redhat/ubi9-minimal:latest

RUN microdnf install -y --nodocs --setopt=install_weak_deps=0 \
        postfix procps-ng cronie expect && \
    microdnf clean all


COPY header_checks /etc/postfix/header_checks
COPY entrypoint.sh /entrypoint.sh
COPY postfix-cron /etc/cron.d/postfix-cron

RUN chmod +x /entrypoint.sh && \
    chmod 0644 /etc/cron.d/postfix-cron && \
    postconf -e "inet_interfaces = all" && \
    postconf -e "inet_protocols = ipv4" && \
    postconf -e "myhostname = postfix-worker.local" && \
    postconf -e "mydestination =" && \
    postconf -e "mynetworks = 127.0.0.0/8, 172.20.0.0/16, 10.0.0.0/16" && \
    postconf -e "smtpd_recipient_restrictions = permit_mynetworks, reject_unauth_destination" && \
    postconf -e "smtpd_peername_lookup = no" && \
    postconf -e "queue_run_delay = 1s" && \
    postconf -e "minimal_backoff_time = 1s" && \
    postconf -e "maximal_backoff_time = 5s" && \
    postconf -e "maximal_queue_lifetime = 6h" && \
    postconf -e "smtp_connect_timeout = 2s" && \
    postconf -e "smtp_destination_concurrency_limit = 50" && \
    postconf -e "message_size_limit = 10485760" && \
    postconf -e "smtp_helo_name = google.cy.bhj.<DOMAIN_C>" && \
    postconf -e "header_checks = regexp:/etc/postfix/header_checks" && \
    postconf -e "recipient_canonical_maps = regexp:/etc/postfix/recipient_canonical" && \
    postconf -e "recipient_canonical_classes = envelope_recipient"

EXPOSE 25

CMD ["/entrypoint.sh"]
