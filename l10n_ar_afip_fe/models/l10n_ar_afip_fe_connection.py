##############################################################################
#
#    Copyright (C) 2007  pronexo.com  (https://www.pronexo.com)
#    All Rights Reserved.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
from odoo import fields, models, api, _
from odoo.exceptions import UserError
from lxml import builder
from lxml import etree
from OpenSSL import crypto
from zeep import transports
import time
import datetime
import base64
import zeep
import logging

_logger = logging.getLogger(__name__)


class ARTransport(transports.Transport):
    def post(self, address, message, headers):
        """ We overwrite this method only to be able to save the xml request and response.
        This will only affect to the connections that are made n this field and it do not extend the original
        Transport class of zeep package.

        NOTE: we try using the HistoryPlugin to save the xml request/reponse but seems this one could have problems when using with multi thread/workers"""
        response = super().post(address, message, headers)
        self.xml_request = etree.tostring(
            etree.fromstring(message), pretty_print=True).decode('utf-8')
        self.xml_response = etree.tostring(
            etree.fromstring(response.content), pretty_print=True).decode('utf-8')
        return response


class L10nArafip_feConnection(models.Model):

    _name = "l10n_ar.afip_fe.connection"
    _description = "AFIP Webservice Connection"
    _rec_name = "l10n_ar_afip_fe"
    _order = "expiration_time desc"

    company_id = fields.Many2one('res.company', required=True, index=True, auto_join=True)
    uniqueid = fields.Char('Unique ID', readonly=True)
    token = fields.Text(readonly=True)
    sign = fields.Text(readonly=True)
    generation_time = fields.Datetime(readonly=True)
    expiration_time = fields.Datetime(readonly=True)
    type = fields.Selection(
        [('production', 'Production'), ('testing', 'Testing')], readonly=True,
        required=True, help='This field is not configure by the user, is extracted from the environment configured in'
        ' the company when the connection was created. It\'s needed because if you change from environment to do quick'
        ' tests we could avoid using the last connection and use the one that matches with the new environment')

    l10n_ar_afip_fe = fields.Selection(selection='_get_l10n_ar_afip_fe', string='AFIP FE', required=True)

    def _get_l10n_ar_afip_fe(self):
        """ Return the list of values of the selection field. """
        return [('wscdc', _('Verification of Invoices (WSCDC)'))] + self.env['account.journal']._get_l10n_ar_afip_fe()

    @api.model
    def _l10n_ar_get_afip_fe_url(self, afip_fe, environment_type):
        """ Function to be inherited on each module that adds a new webservice """
        ws_data = {'wsfe': {'production': "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL",
                            'testing': "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL"},
                   'wsfex': {'production': "https://servicios1.afip.gov.ar/wsfexv1/service.asmx?WSDL",
                             'testing': "https://wswhomo.afip.gov.ar/wsfexv1/service.asmx?WSDL"},
                   'wsbfe': {'production': "https://servicios1.afip.gov.ar/wsbfev1/service.asmx?WSDL",
                             'testing': "https://wswhomo.afip.gov.ar/wsbfev1/service.asmx?WSDL"},
                   'wscdc': {'production': "https://servicios1.afip.gov.ar/WSCDC/service.asmx?WSDL",
                             'testing': "https://wswhomo.afip.gov.ar/WSCDC/service.asmx?WSDL"}}
        return ws_data.get(afip_fe, {}).get(environment_type)

    def _get_client(self, return_transport=False):
        """ Get zeep client to connect to the webservice """
        wsdl = self._l10n_ar_get_afip_fe_url(self.l10n_ar_afip_fe, self.type)
        auth = {'Token': self.token, 'Sign': self.sign, 'Cuit': self.company_id.partner_id.ensure_vat()}
        try:
            transport = ARTransport(operation_timeout=60, timeout=60)
            client = zeep.Client(wsdl, transport=transport)
        except Exception as error:
            self._l10n_ar_process_connection_error(error, self.type, self.l10n_ar_afip_fe)
        if return_transport:
            return client, auth, transport
        return client, auth

    @api.model
    def _l10n_ar_process_connection_error(self, error, env_type, afip_fe):
        """ Review the type of exception received and show a useful message """
        if getattr(error, 'args', ()):
            error_name = error.args[0]
        else:
            error_name = repr(error)

        error_msg = _('There was a problem with the connection to the %s webservice: %s') % (afip_fe, error_name)

        # Find HINT for error message
        hint_msg = False
        certificate_expired = _('It seems like the certificate has expired. Please renew your AFIP certificate')
        token_in_use = 'El CEE ya posee un TA valido para el acceso al WSN solicitado'
        data = {
            'Computador no autorizado a acceder al servicio': _(
                'The certificate is not authorized (delegated) to work with this web service'),
            "ns1:cms.sign.invalid: Firma inválida o algoritmo no soportado": certificate_expired,
            "ns1:cms.cert.expired: Certificado expirado": certificate_expired,
            '500 Server Error: Internal Server': _('Webservice is down'),
            token_in_use: _(
                'Are you invoicing from another computer or system? This error could happen when a access token'
                ' that is requested to AFIP has been requested multiple times and the last one requested is still valid.'
                ' You will need to wait 12 hours to generate a new token and be able to connect to AFIP'
                '\n\n If not, then could be a overload of AFIP service, please wait some time and try again'),
            'No se puede decodificar el BASE64': _('The certificate and private key do not match'),
        }
        for item, value in data.items():
            if item in error_name:
                hint_msg = value
                break

        if token_in_use in error_name and env_type == 'testing':
            hint_msg = _(
                'The testing certificate is been used for another person, you can wait 10 minutes and'
                ' try again or you can change the testing certificate. If this message persist you can:\n\n'
                ' 1) Configure another of the demo certificates pre loaded in demo data'
                ' (On Settings click the ⇒ "Set another demo certificate" button).\n'
                ' 2) Configure your own testing certificates')
        if hint_msg:
            error_msg += '\n\nHINT: ' + hint_msg
        else:
            error_msg += '\n\n' + _('Please report this error to your Odoo provider')
        raise UserError(error_msg)

    def _l10n_ar_get_token_data(self, company, afip_fe):
        """ Call AFIP Authentication webservice to get token & sign data """
        private_key, certificate = company.sudo()._get_key_and_certificate()
        environment_type = company._get_environment_type()
        generation_time = fields.Datetime.now()
        expiration_time = fields.Datetime.add(generation_time, hours=12)
        uniqueId = str(int(time.mktime(datetime.datetime.now().timetuple())))
        request_xml = (builder.E.loginTicketRequest({
            'version': '1.0'},
            builder.E.header(builder.E.uniqueId(uniqueId),
                             builder.E.generationTime(generation_time.strftime('%Y-%m-%dT%H:%M:%S-00:00')),
                             builder.E.expirationTime(expiration_time.strftime('%Y-%m-%dT%H:%M:%S-00:00'))),
            builder.E.service(afip_fe)))
        request = etree.tostring(request_xml, pretty_print=True)

        # sign request
        PKCS7_NOSIGS = 0x4
        pkey = crypto.load_privatekey(crypto.FILETYPE_PEM, private_key)
        signcert = crypto.load_certificate(crypto.FILETYPE_PEM, certificate)
        bio_in = crypto._new_mem_buf(request)
        pkcs7 = crypto._lib.PKCS7_sign(signcert._x509, pkey._pkey, crypto._ffi.NULL, bio_in, PKCS7_NOSIGS)
        bio_out = crypto._new_mem_buf()
        crypto._lib.i2d_PKCS7_bio(bio_out, pkcs7)
        signed_request = crypto._bio_to_string(bio_out)

        wsdl = {'production': "https://wsaa.afip.gov.ar/ws/services/LoginCms?WSDL",
                'testing': "https://wsaahomo.afip.gov.ar/ws/services/LoginCms?WSDL"}.get(environment_type)

        try:
            _logger.info('Connect to AFIP to get token: %s %s %s' % (afip_fe, company.l10n_ar_afip_ws_crt_fname, company.name))
            transport = ARTransport(operation_timeout=60, timeout=60)
            client = zeep.Client(wsdl, transport=transport)
            response = client.service.loginCms(base64.b64encode(signed_request).decode())
        except Exception as error:
            return self._l10n_ar_process_connection_error(error, environment_type, afip_fe)
        response = etree.fromstring(response.encode('utf-8'))

        return {'uniqueid': uniqueId,
                'generation_time': generation_time,
                'expiration_time': expiration_time,
                'token': response.xpath('/loginTicketResponse/credentials/token')[0].text,
                'sign': response.xpath('/loginTicketResponse/credentials/sign')[0].text}