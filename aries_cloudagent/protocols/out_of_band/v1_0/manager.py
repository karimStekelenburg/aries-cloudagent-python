"""Classes to manage connections."""

import asyncio
import logging
import re
from typing import Mapping, Optional, Sequence
import uuid

from aries_cloudagent.core.event_bus import EventBus

from ....connections.base_manager import BaseConnectionManager
from ....connections.models.conn_record import ConnRecord
from ....connections.util import mediation_record_if_id
from ....core.error import BaseError
from ....core.oob_processor import OobMessageProcessor
from ....core.profile import Profile
from ....did.did_key import DIDKey
from ....messaging.decorators.attach_decorator import AttachDecorator
from ....messaging.responder import BaseResponder
from ....multitenant.base import BaseMultitenantManager
from ....storage.error import StorageNotFoundError
from ....transport.inbound.receipt import MessageReceipt
from ....wallet.base import BaseWallet
from ....wallet.key_type import KeyType
from ...connections.v1_0.manager import ConnectionManager
from ...connections.v1_0.messages.connection_invitation import ConnectionInvitation
from ...coordinate_mediation.v1_0.manager import MediationManager
from ...didcomm_prefix import DIDCommPrefix
from ...didexchange.v1_0.manager import DIDXManager
from ...issue_credential.v1_0.models.credential_exchange import V10CredentialExchange
from ...issue_credential.v2_0.models.cred_ex_record import V20CredExRecord
from ...present_proof.v1_0.models.presentation_exchange import V10PresentationExchange
from ...present_proof.v2_0.models.pres_exchange import V20PresExRecord
from .messages.invitation import HSProto, InvitationMessage
from .messages.problem_report import OOBProblemReport
from .messages.reuse import HandshakeReuse
from .messages.reuse_accept import HandshakeReuseAccept
from .messages.service import Service as ServiceMessage
from .models.invitation import InvitationRecord
from .models.oob_record import OobRecord

LOGGER = logging.getLogger(__name__)
REUSE_WEBHOOK_TOPIC = "acapy::webhook::connection_reuse"
REUSE_ACCEPTED_WEBHOOK_TOPIC = "acapy::webhook::connection_reuse_accepted"


class OutOfBandManagerError(BaseError):
    """Out of band error."""


class OutOfBandManagerNotImplementedError(BaseError):
    """Out of band error for unimplemented functionality."""


class OutOfBandManager(BaseConnectionManager):
    """Class for managing out of band messages."""

    def __init__(self, profile: Profile):
        """
        Initialize a OutOfBandManager.

        Args:
            profile: The profile for this out of band manager
        """
        self._profile = profile
        super().__init__(self._profile)

    @property
    def profile(self) -> Profile:
        """
        Accessor for the current profile.

        Returns:
            The profile for this connection manager

        """
        return self._profile

    async def create_invitation(
        self,
        my_label: str = None,
        my_endpoint: str = None,
        auto_accept: bool = None,
        public: bool = False,
        hs_protos: Sequence[HSProto] = None,
        multi_use: bool = False,
        alias: str = None,
        attachments: Sequence[Mapping] = None,
        metadata: dict = None,
        mediation_id: str = None,
    ) -> InvitationRecord:
        """
        Generate new connection invitation.

        This interaction represents an out-of-band communication channel. In the future
        and in practice, these sort of invitations will be received over any number of
        channels such as SMS, Email, QR Code, NFC, etc.

        Args:
            my_label: label for this connection
            my_endpoint: endpoint where other party can reach me
            auto_accept: auto-accept a corresponding connection request
                (None to use config)
            public: set to create an invitation from the public DID
            hs_protos: list of handshake protocols to include
            multi_use: set to True to create an invitation for multiple-use connection
            alias: optional alias to apply to connection for later use
            attachments: list of dicts in form of {"id": ..., "type": ...}

        Returns:
            Invitation record

        """
        mediation_mgr = MediationManager(self.profile)
        mediation_record = await mediation_record_if_id(
            self.profile,
            mediation_id,
            or_default=True,
        )
        keylist_updates = None

        if not (hs_protos or attachments):
            raise OutOfBandManagerError(
                "Invitation must include handshake protocols, "
                "request attachments, or both"
            )

        # Multitenancy setup
        multitenant_mgr = self.profile.inject_or(BaseMultitenantManager)
        wallet_id = self.profile.settings.get("wallet.id")

        accept = bool(
            auto_accept
            or (
                auto_accept is None
                and self.profile.settings.get("debug.auto_accept_requests")
            )
        )
        if public:
            if multi_use:
                raise OutOfBandManagerError(
                    "Cannot create public invitation with multi_use"
                )
            if metadata:
                raise OutOfBandManagerError(
                    "Cannot store metadata on public invitations"
                )

        if attachments and multi_use:
            raise OutOfBandManagerError(
                "Cannot create multi use invitation with attachments"
            )

        invitation_message_id = str(uuid.uuid4())

        message_attachments = []
        for atch in attachments or []:
            a_type = atch.get("type")
            a_id = atch.get("id")

            message = None

            if a_type == "credential-offer":
                try:
                    async with self.profile.session() as session:
                        cred_ex_rec = await V10CredentialExchange.retrieve_by_id(
                            session,
                            a_id,
                        )
                        message = cred_ex_rec.credential_offer_dict.serialize()

                except StorageNotFoundError:
                    async with self.profile.session() as session:
                        cred_ex_rec = await V20CredExRecord.retrieve_by_id(
                            session,
                            a_id,
                        )
                        message = cred_ex_rec.cred_offer.serialize()
            elif a_type == "present-proof":
                try:
                    async with self.profile.session() as session:
                        pres_ex_rec = await V10PresentationExchange.retrieve_by_id(
                            session,
                            a_id,
                        )
                        message = pres_ex_rec.presentation_request_dict.serialize()
                except StorageNotFoundError:
                    async with self.profile.session() as session:
                        pres_ex_rec = await V20PresExRecord.retrieve_by_id(
                            session,
                            a_id,
                        )
                        message = pres_ex_rec.pres_request.serialize()
            else:
                raise OutOfBandManagerError(f"Unknown attachment type: {a_type}")

            # Assign pthid to the attached message
            message["~thread"] = {
                **message.get("~thread", {}),
                "pthid": invitation_message_id,
            }
            message_attachments.append(InvitationMessage.wrap_message(message))

        handshake_protocols = [
            DIDCommPrefix.qualify_current(hsp.name) for hsp in hs_protos or []
        ] or None
        connection_protocol = (
            hs_protos[0].name if hs_protos and len(hs_protos) >= 1 else None
        )

        our_recipient_key = None
        conn_rec = None

        if public:
            if not self.profile.settings.get("public_invites"):
                raise OutOfBandManagerError("Public invitations are not enabled")

            async with self.profile.session() as session:
                wallet = session.inject(BaseWallet)
                public_did = await wallet.get_public_did()
            if not public_did:
                raise OutOfBandManagerError(
                    "Cannot create public invitation with no public DID"
                )

            invi_msg = InvitationMessage(  # create invitation message
                _id=invitation_message_id,
                label=my_label or self.profile.settings.get("default_label"),
                handshake_protocols=handshake_protocols,
                requests_attach=message_attachments,
                services=[f"did:sov:{public_did.did}"],
            )
            keylist_updates = await mediation_mgr.add_key(
                public_did.verkey, keylist_updates
            )

            our_recipient_key = public_did.verkey

            endpoint, *_ = await self.resolve_invitation(public_did.did)
            invi_url = invi_msg.to_url(endpoint)

            if multitenant_mgr and wallet_id:  # add mapping for multitenant relay
                await multitenant_mgr.add_key(
                    wallet_id, public_did.verkey, skip_if_exists=True
                )

            # Only create connection record if hanshake_protocols is defined
            if handshake_protocols:
                conn_rec = ConnRecord(  # create connection record
                    invitation_key=public_did.verkey,
                    invitation_msg_id=invi_msg._id,
                    their_role=ConnRecord.Role.REQUESTER.rfc23,
                    state=ConnRecord.State.INVITATION.rfc23,
                    accept=ConnRecord.ACCEPT_AUTO
                    if accept
                    else ConnRecord.ACCEPT_MANUAL,
                    alias=alias,
                    connection_protocol=connection_protocol,
                )

                async with self.profile.session() as session:
                    await conn_rec.save(session, reason="Created new invitation")
                    await conn_rec.attach_invitation(session, invi_msg)

        else:
            if not my_endpoint:
                my_endpoint = self.profile.settings.get("default_endpoint")

            # Create and store new key for exchange
            async with self.profile.session() as session:
                wallet = session.inject(BaseWallet)
                connection_key = await wallet.create_signing_key(KeyType.ED25519)
            keylist_updates = await mediation_mgr.add_key(
                connection_key.verkey, keylist_updates
            )

            our_recipient_key = connection_key.verkey
            # Add mapping for multitenant relay
            if multitenant_mgr and wallet_id:
                await multitenant_mgr.add_key(wallet_id, connection_key.verkey)
            # Initializing  InvitationMessage here to include
            # invitation_msg_id in webhook poyload
            invi_msg = InvitationMessage(_id=invitation_message_id)

            if handshake_protocols:
                invitation_mode = (
                    ConnRecord.INVITATION_MODE_MULTI
                    if multi_use
                    else ConnRecord.INVITATION_MODE_ONCE
                )
                # Create connection record
                conn_rec = ConnRecord(
                    invitation_key=connection_key.verkey,
                    their_role=ConnRecord.Role.REQUESTER.rfc23,
                    state=ConnRecord.State.INVITATION.rfc23,
                    accept=ConnRecord.ACCEPT_AUTO
                    if accept
                    else ConnRecord.ACCEPT_MANUAL,
                    invitation_mode=invitation_mode,
                    alias=alias,
                    connection_protocol=connection_protocol,
                    invitation_msg_id=invi_msg._id,
                )

                async with self.profile.session() as session:
                    await conn_rec.save(session, reason="Created new connection")

            routing_keys = []
            # The base wallet can act as a mediator for all tenants
            if multitenant_mgr and wallet_id:
                base_mediation_record = await multitenant_mgr.get_default_mediator()

                if base_mediation_record:
                    routing_keys = base_mediation_record.routing_keys
                    my_endpoint = base_mediation_record.endpoint

                    # If we use a mediator for the base wallet we don't
                    # need to register the key at the subwallet mediator
                    # because it only needs to know the key of the base mediator
                    # sub wallet mediator -> base wallet mediator -> agent
                    keylist_updates = None
            if mediation_record:
                routing_keys = [*routing_keys, *mediation_record.routing_keys]
                my_endpoint = mediation_record.endpoint

                # Save that this invitation was created with mediation

                async with self.profile.session() as session:
                    await conn_rec.metadata_set(
                        session,
                        MediationManager.METADATA_KEY,
                        {MediationManager.METADATA_ID: mediation_record.mediation_id},
                    )

                if keylist_updates:
                    responder = self.profile.inject_or(BaseResponder)
                    await responder.send(
                        keylist_updates, connection_id=mediation_record.connection_id
                    )
            routing_keys = [
                key
                if len(key.split(":")) == 3
                else DIDKey.from_public_key_b58(key, KeyType.ED25519).did
                for key in routing_keys
            ]
            # Create connection invitation message
            # Note: Need to split this into two stages to support inbound routing
            # of invitations
            # Would want to reuse create_did_document and convert the result
            invi_msg.label = my_label or self.profile.settings.get("default_label")
            invi_msg.handshake_protocols = handshake_protocols
            invi_msg.requests_attach = message_attachments
            invi_msg.services = [
                ServiceMessage(
                    _id="#inline",
                    _type="did-communication",
                    recipient_keys=[
                        DIDKey.from_public_key_b58(
                            connection_key.verkey, KeyType.ED25519
                        ).did
                    ],
                    service_endpoint=my_endpoint,
                    routing_keys=routing_keys,
                )
            ]
            invi_url = invi_msg.to_url()

            # Update connection record
            if conn_rec:
                async with self.profile.session() as session:
                    await conn_rec.attach_invitation(session, invi_msg)

                    if metadata:
                        for key, value in metadata.items():
                            await conn_rec.metadata_set(session, key, value)

        oob_record = OobRecord(
            role=OobRecord.ROLE_SENDER,
            state=OobRecord.STATE_AWAIT_RESPONSE,
            connection_id=conn_rec.connection_id if conn_rec else None,
            invi_msg_id=invi_msg._id,
            invitation=invi_msg,
            our_recipient_key=our_recipient_key,
        )

        async with self.profile.session() as session:
            await oob_record.save(session, reason="Created new oob invitation")

        return InvitationRecord(  # for return via admin API, not storage
            oob_id=oob_record.oob_id,
            state=InvitationRecord.STATE_INITIAL,
            invi_msg_id=invi_msg._id,
            invitation=invi_msg,
            invitation_url=invi_url,
        )

    async def receive_invitation(
        self,
        invitation: InvitationMessage,
        use_existing_connection: bool = True,
        auto_accept: Optional[bool] = None,
        alias: Optional[str] = None,
        mediation_id: Optional[str] = None,
    ) -> OobRecord:
        """
        Receive an out of band invitation message.

        Args:
            invitation: invitation message
            use_existing_connection: whether to use existing connection if possible
            auto_accept: whether to accept the invitation automatically
            alias: Alias for connection record
            mediation_id: mediation identifier

        Returns:
            ConnRecord, serialized

        """
        if mediation_id:
            try:
                await mediation_record_if_id(self.profile, mediation_id)
            except StorageNotFoundError:
                mediation_id = None

        # There must be exactly 1 service entry
        if len(invitation.services) != 1:
            raise OutOfBandManagerError("service array must have exactly one element")

        if not (invitation.requests_attach or invitation.handshake_protocols):
            raise OutOfBandManagerError(
                "Invitation must specify handshake_protocols, requests_attach, or both"
            )

        # Get the single service item
        oob_service_item = invitation.services[0]

        # Get the DID public did, if any
        public_did = None
        if isinstance(oob_service_item, str):
            public_did = oob_service_item.split(":")[-1]

        conn_rec = None

        # Find existing connection - only if started by an invitation with Public DID
        # and use_existing_connection is true
        if (
            public_did is not None and use_existing_connection
        ):  # invite has public DID: seek existing connection
            LOGGER.debug(
                f"Trying to find existing connection for oob invitation with did {public_did}"
            )
            async with self._profile.session() as session:
                conn_rec = await ConnRecord.find_existing_connection(
                    session=session, their_public_did=public_did
                )

        oob_record = OobRecord(
            role=OobRecord.ROLE_RECEIVER,
            invi_msg_id=invitation._id,
            invitation=invitation,
            state=OobRecord.STATE_INITIAL,
            connection_id=conn_rec.connection_id if conn_rec else None,
        )

        # Save record
        # TODO: I think we can remove this save. Other paths will save the record
        async with self.profile.session() as session:
            await oob_record.save(session)

        # Try to reuse the connection. If not accepted sets the conn_rec to None
        if conn_rec and not invitation.requests_attach:
            oob_record = await self._handle_hanshake_reuse(oob_record, conn_rec)
            conn_rec = None

            LOGGER.warning(
                f"Connection reuse request finished with state {oob_record.state}"
            )

            # If reuse is accepted we can return as the oob exchange is complete
            # TODO: update the state to DONE
            # TODO: Should we remove the oob record if the reuse has been accepted?
            if oob_record.state == OobRecord.STATE_ACCEPTED:
                return oob_record

        # Try to create a connection. Either if the reuse failed or we didn't have a connection yet
        # Throws an error if connection could not be created
        if not conn_rec and invitation.handshake_protocols:
            oob_record = await self._perform_handshake(
                oob_record=oob_record,
                alias=alias,
                auto_accept=auto_accept,
                mediation_id=mediation_id,
            )
            LOGGER.debug(
                f"Performed handshake with connection {oob_record.connection_id}"
            )
            # re-fetch connection record
            async with self.profile.session() as session:
                conn_rec = await ConnRecord.retrieve_by_id(
                    session, oob_record.connection_id
                )

        # Handle any attachments
        if invitation.requests_attach:
            LOGGER.debug(
                f"Process attached messages for oob exchange {oob_record.oob_id} (connection_id {oob_record.connection_id})"
            )
            if oob_record.connection_id:
                # Wait for connection to become active.
                # FIXME: this should ideally be handled using an event handler. Once the connection is ready
                # we start processing the attached messages. For now we use the timeout method
                # TODO: what if not ready within the timeout?
                await self._wait_for_conn_rec_active(oob_record.connection_id)
            if not conn_rec:
                # Create and store new key for connectionless exchange
                async with self.profile.session() as session:
                    wallet = session.inject(BaseWallet)
                    connection_key = await wallet.create_signing_key(KeyType.ED25519)
                    oob_record.our_recipient_key = connection_key.verkey
                    await oob_record.save(session)

            await self._respond_request_attach(oob_record)

        # TODO: remove record? not possible with connectionless
        oob_record.state = OobRecord.STATE_DONE

        return oob_record

    async def _respond_request_attach(self, oob_record: OobRecord):
        invitation = oob_record.invitation
        req_attach = invitation.requests_attach[0]

        if not isinstance(req_attach, AttachDecorator):
            raise OutOfBandManagerError("requests~attach is not properly formatted")

        message_processor = self.profile.inject(OobMessageProcessor)

        LOGGER.warning("Handle inbound oob message")

        # TODO: should we add somethign to get the outcome of processing the message?
        # Success will happen through protocol specific webhooks
        await message_processor.handle_message(
            self.profile,
            req_attach.content,
            oob_record=oob_record,
        )

    async def _wait_for_reuse_response(
        self, oob_id: str, timeout: int = 15
    ) -> OobRecord:
        """
        Wait for reuse response message state. Either by receiving a reuse accepted or problem
        report. If no answer is received withing the timeout, the state will be set to reuse_not_acceted

        Args:
            oob_id: Identifier of the oob record
            timeout: The timeout in seconds to wait for the reuse state [default=15]

        Returns:

        """
        OOB_REUSE_RESPONSE_STATE = re.compile(
            "^acapy::record::out_of_band::(reuse_accepted|reuse_not_accepted)$"
        )

        async def _wait_for_state() -> OobRecord:
            event = self.profile.inject(EventBus)
            with event.wait_for_event(
                self.profile,
                OOB_REUSE_RESPONSE_STATE,
                lambda event: event.payload.get("oob_id") == oob_id,
            ) as await_event:
                # After starting the listener first retrieve the record from storage.
                # This rules out the scenario where the record was in the desired state
                # Before starting the event listener
                async with self.profile.session() as session:
                    oob_record = await OobRecord.retrieve_by_id(session, oob_id)

                    if oob_record.state in [
                        OobRecord.STATE_ACCEPTED,
                        OobRecord.STATE_NOT_ACCEPTED,
                    ]:
                        return oob_record

                LOGGER.debug(f"Wait for oob {oob_id} to receive reuse accepted mesage")
                # Wait for oob_record to have reuse_accepted state
                event = await await_event
                return OobRecord.deserialize(event.payload)

        try:
            oob_record = await asyncio.wait_for(
                _wait_for_state(),
                timeout,
            )

            return oob_record
        except asyncio.TimeoutError:
            async with self.profile.session() as session:
                oob_record = await OobRecord.retrieve_by_id(session, oob_id)
                return oob_record

    async def _wait_for_conn_rec_active(
        self, connection_id: str, timeout: int = 7
    ) -> Optional[ConnRecord]:
        CONNECTION_READY_EVENT = re.compile(
            "^acapy::record::connections::(active|completed|response)$"
        )

        LOGGER.debug(f"Wait for connection {connection_id} to become active")

        async def _wait_for_state() -> ConnRecord:
            event = self.profile.inject(EventBus)
            with event.wait_for_event(
                self.profile,
                CONNECTION_READY_EVENT,
                lambda event: event.payload.get("connection_id") == connection_id,
            ) as await_event:
                # After starting the listener first retrieve the record from storage.
                # This rules out the scenario where the record was in the desired state
                # Before starting the event listener
                async with self.profile.session() as session:
                    conn_record = await ConnRecord.retrieve_by_id(
                        session, connection_id
                    )
                    if conn_record.is_ready:
                        return conn_record

                LOGGER.debug(f"Wait for connection {connection_id} to become active")
                # Wait for connection record to be in state
                event = await await_event
                return ConnRecord.deserialize(event.payload)

        try:
            return await asyncio.wait_for(
                _wait_for_state(),
                timeout,
            )

        except asyncio.TimeoutError:
            LOGGER.warning(f"Connection for connection_id {connection_id} not ready")
            return None

    async def _handle_hanshake_reuse(
        self, oob_record: OobRecord, conn_record: ConnRecord
    ) -> OobRecord:
        # Send handshake reuse
        oob_record = await self._create_handshake_reuse_message(oob_record, conn_record)

        # Wait for the reuse accepted message
        oob_record = await self._wait_for_reuse_response(oob_record.oob_id)
        LOGGER.debug(
            f"Oob reuse for oob id {oob_record.oob_id} with connection {oob_record.connection_id} finished with state {oob_record.state}"
        )

        if oob_record.state != OobRecord.STATE_ACCEPTED:
            # Remove associated connection id as reuse has ben denied
            oob_record.connection_id = None

            # TODO: replace webhook event with new oob webhook event
            # Emit webhook if the reuse was not accepted
            await self.profile.notify(
                REUSE_ACCEPTED_WEBHOOK_TOPIC,
                {
                    "thread_id": oob_record.reuse_msg_id,
                    "connection_id": conn_record.connection_id,
                    "state": "rejected",
                    "comment": (
                        "No HandshakeReuseAccept message received, "
                        f"connection {conn_record.connection_id} ",
                        f"and invitation {oob_record.invitation._id}",
                    ),
                },
            )

            async with self.profile.session() as session:
                await oob_record.save(session)

        return oob_record

    async def _perform_handshake(
        self,
        *,
        oob_record: OobRecord,
        alias: Optional[str] = None,
        auto_accept: Optional[bool] = None,
        mediation_id: Optional[str] = None,
    ) -> OobRecord:
        invitation = oob_record.invitation

        supported_handshake_protocols = [
            HSProto.get(hsp)
            for hsp in dict.fromkeys(
                [
                    DIDCommPrefix.unqualify(proto)
                    for proto in invitation.handshake_protocols
                ]
            )
        ]

        # Get the single service item
        service = invitation.services[0]
        public_did = None
        if isinstance(service, str):
            # If it's in the did format, we need to convert to a full service block
            # An existing connection can only be reused based on a public DID
            # in an out-of-band message (RFC 0434).

            public_did = service.split(":")[-1]

            # TODO: resolve_invitation should resolve key_info objects
            # or something else that includes the key type. We now assume
            # ED25519 keys
            endpoint, recipient_keys, routing_keys = await self.resolve_invitation(
                service
            )
            service = ServiceMessage.deserialize(
                {
                    "id": "#inline",
                    "type": "did-communication",
                    "recipientKeys": [
                        DIDKey.from_public_key_b58(key, KeyType.ED25519).did
                        for key in recipient_keys
                    ],
                    "routingKeys": [
                        DIDKey.from_public_key_b58(key, KeyType.ED25519).did
                        for key in routing_keys
                    ],
                    "serviceEndpoint": endpoint,
                }
            )

        LOGGER.debug(f"Creating connection with public did {public_did}")

        conn_record = None
        for protocol in supported_handshake_protocols:
            # DIDExchange
            if protocol is HSProto.RFC23:
                didx_mgr = DIDXManager(self.profile)
                conn_record = await didx_mgr.receive_invitation(
                    invitation=invitation,
                    their_public_did=public_did,
                    auto_accept=auto_accept,
                    alias=alias,
                    mediation_id=mediation_id,
                )
                break
            # 0160 Connection
            elif protocol is HSProto.RFC160:
                service.recipient_keys = [
                    DIDKey.from_did(key).public_key_b58
                    for key in service.recipient_keys or []
                ]
                service.routing_keys = [
                    DIDKey.from_did(key).public_key_b58 for key in service.routing_keys
                ] or []
                connection_invitation = ConnectionInvitation.deserialize(
                    {
                        "@id": invitation._id,
                        "@type": DIDCommPrefix.qualify_current(protocol.name),
                        "label": invitation.label,
                        "recipientKeys": service.recipient_keys,
                        "serviceEndpoint": service.service_endpoint,
                        "routingKeys": service.routing_keys,
                    }
                )
                conn_mgr = ConnectionManager(self.profile)
                conn_record = await conn_mgr.receive_invitation(
                    invitation=connection_invitation,
                    their_public_did=public_did,
                    auto_accept=auto_accept,
                    alias=alias,
                    mediation_id=mediation_id,
                )
                break

        if not conn_record:
            raise OutOfBandManagerError(
                f"Unable to create connection. Could not perform handshake using any of the handshake_protocols (supported {supported_handshake_protocols})"
            )

        async with self.profile.session() as session:
            oob_record.connection_id = conn_record.connection_id
            await oob_record.save(session)

        return oob_record

    async def _create_handshake_reuse_message(
        self,
        oob_record: OobRecord,
        conn_record: ConnRecord,
    ) -> OobRecord:
        """
        Create and Send a Handshake Reuse message under RFC 0434.

        Args:
            oob_record: OOB  Record
            conn_record: Connection record associated with the oob record

        Returns:

        Raises:
            OutOfBandManagerError: If there is an issue creating or
            sending the OOB invitation

        """
        try:
            reuse_msg = HandshakeReuse()
            reuse_msg.assign_thread_id(thid=reuse_msg._id, pthid=oob_record.invi_msg_id)

            connection_targets = await self.fetch_connection_targets(
                connection=conn_record
            )

            responder = self.profile.inject(BaseResponder)
            await responder.send(
                message=reuse_msg,
                target_list=connection_targets,
            )

            async with self.profile.session() as session:
                oob_record.reuse_msg_id = reuse_msg._id
                oob_record.state = OobRecord.STATE_AWAIT_RESPONSE
                await oob_record.save(session, reason="Storing reuse msg data")

            return oob_record

        except Exception as err:
            raise OutOfBandManagerError(
                f"Error on creating and sending a handshake reuse message: {err}"
            )

    async def delete_stale_connection_by_invitation(self, invi_msg_id: str):
        """Delete unused connections, using existing an active connection instead."""
        tag_filter = {
            "invitation_msg_id": invi_msg_id,
        }
        post_filter = {"invitation_mode": "once", "state": "invitation"}

        async with self.profile.session() as session:
            conn_records = await ConnRecord.query(
                session,
                tag_filter=tag_filter,
                post_filter_positive=post_filter,
            )
            for conn_rec in conn_records:
                await conn_rec.delete_record(session)

    async def receive_reuse_message(
        self,
        reuse_msg: HandshakeReuse,
        receipt: MessageReceipt,
        conn_rec: ConnRecord,
    ) -> None:
        """
        Receive and process a HandshakeReuse message under RFC 0434.

        Process a `HandshakeReuse` message by looking up
        the connection records using the MessageReceipt sender DID.

        Args:
            reuse_msg: The `HandshakeReuse` to process
            receipt: The message receipt

        Returns:

        Raises:
            OutOfBandManagerError: If the existing connection is not active
            or the connection does not exists

        """
        invi_msg_id = reuse_msg._thread.pthid
        reuse_msg_id = reuse_msg._thread_id

        reuse_accept_msg = HandshakeReuseAccept()
        reuse_accept_msg.assign_thread_id(thid=reuse_msg_id, pthid=invi_msg_id)
        connection_targets = await self.fetch_connection_targets(connection=conn_rec)

        responder = self.profile.inject(BaseResponder)

        # Update ConnRecord's invi_msg_id
        async with self._profile.session() as session:
            oob_record = await OobRecord.retrieve_by_tag_filter(
                session,
                {"invi_msg_id": invi_msg_id},
                {"state": OobRecord.STATE_AWAIT_RESPONSE},
            )

            oob_record.state = OobRecord.STATE_ACCEPTED
            oob_record.reuse_msg_id = reuse_msg_id
            oob_record.connection_id = conn_rec.connection_id
            await oob_record.save(session)

            conn_rec.invitation_msg_id = invi_msg_id
            await conn_rec.save(session, reason="Assigning new invitation_msg_id")

        # Delete the ConnRecord created; re-use existing connection
        await self.delete_stale_connection_by_invitation(invi_msg_id)
        # Emit webhook
        await self.profile.notify(
            REUSE_WEBHOOK_TOPIC,
            {
                "thread_id": reuse_msg_id,
                "connection_id": conn_rec.connection_id,
                "comment": (
                    f"Connection {conn_rec.connection_id} is being reused "
                    f"for invitation {invi_msg_id}"
                ),
            },
        )

        await responder.send(
            message=reuse_accept_msg,
            target_list=connection_targets,
        )

    async def receive_reuse_accepted_message(
        self,
        reuse_accepted_msg: HandshakeReuseAccept,
        receipt: MessageReceipt,
        conn_record: ConnRecord,
    ) -> None:
        """
        Receive and process a HandshakeReuseAccept message under RFC 0434.

        Process a `HandshakeReuseAccept` message by updating the OobRecord
        state to `accepted`.

        Args:
            reuse_accepted_msg: The `HandshakeReuseAccept` to process
            receipt: The message receipt

        Returns:

        Raises:
            OutOfBandManagerError: if there is an error in processing the
            HandshakeReuseAccept message

        """
        invi_msg_id = reuse_accepted_msg._thread.pthid
        thread_reuse_msg_id = reuse_accepted_msg._thread.thid

        try:
            async with self.profile.session() as session:
                oob_record = await OobRecord.retrieve_by_tag_filter(
                    session,
                    {"invi_msg_id": invi_msg_id, "reuse_msg_id": thread_reuse_msg_id},
                )

                oob_record.state = OobRecord.STATE_ACCEPTED
                conn_record.invitation_msg_id = invi_msg_id
                await conn_record.save(
                    session, reason="Assigning new invitation_msg_id"
                )
                await oob_record.save(session, reason="Reuse accepted")
            # Emit webhook
            await self.profile.notify(
                REUSE_ACCEPTED_WEBHOOK_TOPIC,
                {
                    "thread_id": thread_reuse_msg_id,
                    "connection_id": conn_record.connection_id,
                    "state": "accepted",
                    "comment": (
                        f"Connection {conn_record.connection_id} is being reused "
                        f"for invitation {invi_msg_id}"
                    ),
                },
            )
        except Exception as e:
            # Emit webhook
            await self.profile.notify(
                REUSE_ACCEPTED_WEBHOOK_TOPIC,
                {
                    "thread_id": thread_reuse_msg_id,
                    "connection_id": conn_record.connection_id,
                    "state": "rejected",
                    "comment": (
                        "Unable to process HandshakeReuseAccept message, "
                        f"connection {conn_record.connection_id} "
                        f"and invitation {invi_msg_id}"
                    ),
                },
            )
            raise OutOfBandManagerError(
                (
                    (
                        "Error processing reuse accepted message "
                        f"for OOB invitation {invi_msg_id}, {e}"
                    )
                )
            )

    async def receive_problem_report(
        self,
        problem_report: OOBProblemReport,
        receipt: MessageReceipt,
        conn_record: ConnRecord,
    ) -> None:
        """
        Receive and process a ProblemReport message from the inviter to invitee.

        Process a `ProblemReport` message by updating the OobRecord
        state to `not_accepted`.

        Args:
            problem_report: The `OOBProblemReport` to process
            receipt: The message receipt

        Returns:

        Raises:
            OutOfBandManagerError: if there is an error in processing the
            HandshakeReuseAccept message

        """
        invi_msg_id = problem_report._thread.pthid
        thread_reuse_msg_id = problem_report._thread.thid
        try:
            async with self.profile.session() as session:
                oob_record = await OobRecord.retrieve_by_tag_filter(
                    session,
                    {"invi_msg_id": invi_msg_id, "reuse_msg_id": thread_reuse_msg_id},
                )
                oob_record.state = OobRecord.STATE_NOT_ACCEPTED
                await oob_record.save(session)
        except Exception as e:
            raise OutOfBandManagerError(
                (
                    (
                        "Error processing problem report message "
                        f"for OOB invitation {invi_msg_id}, {e}"
                    )
                )
            )
