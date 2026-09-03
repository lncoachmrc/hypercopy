import type { Metadata } from "next";
import styles from "../../privacy/privacy.module.css";

export const metadata: Metadata = {
  title: "Términos y Condiciones | TRAXION",
  description:
    "Términos y Condiciones de TRAXION para acceso, prueba, suscripciones, uso de Hyperliquid, riesgo operativo y derechos de los consumidores.",
  robots: { index: true, follow: true },
  alternates: {
    canonical: "https://traxion.lucianonovello.com/terms/es/",
    languages: {
      "it-IT": "https://traxion.lucianonovello.com/terms/",
      "es-ES": "https://traxion.lucianonovello.com/terms/es/",
    },
  },
};

export default function SpanishTermsAndConditionsPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <a className={styles.brand} href="/">TRAXION</a>
          <a className={styles.back} href="/terms/">Italiano</a>
          <a className={styles.back} href="/">← Volver al sitio</a>
        </div>
      </header>

      <main className={styles.main}>
        <div className={styles.hero}>
          <p className={styles.kicker}>Condiciones de uso y suscripción</p>
          <h1>Términos y Condiciones</h1>
          <p className={styles.lead}>
            Estos Términos regulan el acceso y el uso de TRAXION por consumidores, profesionales y empresas,
            incluidas la prueba gratuita, los planes de pago y las funcionalidades conectadas con Hyperliquid.
          </p>
        </div>

        <div className={styles.notice}>
          <strong>Estado de los datos del Proveedor</strong>
          Los datos identificativos completos del proveedor están en proceso de completarse. Antes de la puesta en
          servicio comercial definitiva se publicarán la denominación fiscal, NIF/NIE, domicilio profesional y un
          contacto contractual dedicado. Estos Términos ya están preparados para incorporar esos datos sin alterar
          las reglas sustanciales del servicio.
        </div>

        <div className={styles.content}>
          <section className={styles.section}>
            <h2>1. Ámbito y aceptación</h2>
            <p>
              Estos Términos y Condiciones regulan el acceso a los sitios, a la aplicación web y a los servicios
              tecnológicos TRAXION, incluida la autenticación mediante wallet, la configuración del Risk Engine,
              la conexión de un Agent/API Wallet de Hyperliquid, las funciones de análisis y los procesos de
              ejecución.
            </p>
            <p>
              Al utilizar el servicio o adquirir un plan, el usuario acepta la versión de los Términos que se le
              haya facilitado antes de celebrar el contrato. Cuando el usuario actúe como consumidor, se mantienen
              íntegramente los derechos imperativos reconocidos por la normativa de protección de consumidores.
            </p>
          </section>

          <section className={styles.section}>
            <h2>2. Proveedor del servicio</h2>
            <p>
              El Proveedor es el sujeto que gestiona TRAXION. La denominación fiscal, NIF/NIE, domicilio profesional
              y contacto contractual están en proceso de completarse y se publicarán en esta sección antes de la
              puesta en servicio comercial definitiva.
            </p>
          </section>

          <section className={styles.section}>
            <h2>3. Definiciones esenciales</h2>
            <ul>
              <li><strong>TRAXION</strong>: el servicio tecnológico descrito en estos Términos.</li>
              <li><strong>Usuario</strong>: la persona física o jurídica que accede a TRAXION o lo utiliza.</li>
              <li><strong>Consumidor</strong>: la persona física que actúa con fines ajenos a su actividad empresarial o profesional.</li>
              <li><strong>Profesional</strong>: quien actúa en el marco de su actividad comercial, empresarial o profesional.</li>
              <li><strong>Cuenta Hyperliquid</strong>: la cuenta externa en la que permanecen los fondos y posiciones del usuario.</li>
              <li><strong>Agent/API Wallet</strong>: credencial dedicada autorizada por el usuario para las operaciones técnicamente permitidas.</li>
              <li><strong>Risk Engine</strong>: conjunto de reglas deterministas que limita o bloquea determinadas acciones operativas.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>4. Requisitos de uso</h2>
            <p>
              El usuario persona física debe tener al menos 18 años y plena capacidad para contratar. Quien utilice
              TRAXION en nombre de una sociedad u otra organización declara disponer de facultades suficientes para
              vincular a dicha entidad a estos Términos.
            </p>
            <p>
              El usuario debe verificar que el uso de TRAXION, Hyperliquid, activos digitales y perpetuals sea
              lícito en su jurisdicción y respetar las condiciones aplicables de los servicios de terceros. TRAXION
              puede no estar disponible en todas las jurisdicciones.
            </p>
          </section>

          <section className={styles.section}>
            <h2>5. Naturaleza del servicio</h2>
            <p>
              TRAXION es un sistema tecnológico que conecta inteligencia humana, componentes de Capital
              Intelligence AI, un Risk Engine y una capa de ejecución. El servicio puede transformar estrategias y
              señales estructuradas en objetivos operativos, comprobar límites de riesgo, calcular diferencias con
              respecto al estado real de la cuenta y, cuando el usuario haya activado la ejecución, enviar
              operaciones autorizadas.
            </p>
            <p>
              TRAXION no promete rentabilidad, resultados, disponibilidad de mercado ni protección frente a
              pérdidas. La información y las funciones del servicio no sustituyen una valoración autónoma del
              riesgo ni el asesoramiento profesional independiente que pueda resultar necesario para el usuario.
            </p>
          </section>

          <section className={styles.section}>
            <h2>6. Modelo no custodial</h2>
            <p>
              Los fondos permanecen en la Cuenta Hyperliquid del usuario. TRAXION no recibe los fondos en custodia
              ni implementa funciones de retirada. Las operaciones previstas por el servicio se realizan mediante
              las autorizaciones técnicas concedidas al Agent/API Wallet dedicado.
            </p>
            <p>
              La separación técnica entre el wallet principal y el Agent/API Wallet no elimina los riesgos de
              trading, protocolo, exchange, red o compromiso de credenciales.
            </p>
          </section>

          <section className={styles.section}>
            <h2>7. Wallet principal y Agent/API Wallet</h2>
            <p>
              El usuario nunca debe comunicar a TRAXION la seed phrase ni la clave privada de su wallet principal.
              Para la ejecución debe utilizarse exclusivamente un Agent/API Wallet dedicado y autorizado conforme a
              los procedimientos de Hyperliquid.
            </p>
            <p>
              El usuario es responsable de custodiar sus credenciales, comprobar las direcciones vinculadas y
              revocar o sustituir el Agent si sospecha que ha sido comprometido. Revocar el Agent interrumpe su
              autoridad operativa, pero no cancela automáticamente una suscripción.
            </p>
          </section>

          <section className={styles.section}>
            <h2>8. Autenticación y seguridad de la cuenta</h2>
            <p>
              El acceso puede realizarse mediante firma criptográfica del wallet y sesiones de aplicación. El
              usuario debe proteger el dispositivo, navegador, wallet y cualquier instrumento utilizado para
              acceder al servicio. Los registros técnicos de autenticación y actividad podrán utilizarse como
              elementos de verificación de los hechos, sin alterar las reglas legales sobre prueba ni los derechos
              imperativos del consumidor.
            </p>
          </section>

          <section className={styles.section}>
            <h2>9. Risk Engine, SHADOW y ejecución automatizada</h2>
            <p>
              TRAXION puede aplicar límites relativos, entre otros, a exposición, apalancamiento, drawdown, pérdida
              diaria, número de posiciones, mercados permitidos y dimensionamiento. Los controles pueden permitir,
              reducir, denegar o aplazar una acción.
            </p>
            <p>
              El modo SHADOW permite calcular objetivos, sizing y controles sin enviar órdenes. Cuando el usuario
              activa la ejecución real, autoriza al servicio a enviar operaciones compatibles con la configuración
              activa y con los límites técnicos aplicables.
            </p>
          </section>

          <section className={styles.section}>
            <h2>10. Funciones de inteligencia artificial</h2>
            <p>
              Los componentes de IA pueden analizar datos y generar información estructurada, prioridades o
              evaluaciones de contexto. Pueden cometer errores, producir resultados incompletos o no reflejar
              acontecimientos de mercado posteriores.
            </p>
            <p>
              La IA no constituye directamente la autoridad técnica final para firmar órdenes. La ejecución permanece
              separada y sujeta a las reglas deterministas y autorizaciones previstas por la arquitectura del servicio.
            </p>
          </section>

          <section className={styles.section}>
            <h2>11. Obligaciones del usuario</h2>
            <p>El usuario se compromete a:</p>
            <ul>
              <li>facilitar datos y configuraciones correctos cuando sean necesarios;</li>
              <li>proteger wallets, dispositivos y Agent/API Wallet;</li>
              <li>comprobar periódicamente el estado de la cuenta, posiciones, límites y autorizaciones;</li>
              <li>mantener un nivel de riesgo coherente con su situación y capacidad de pérdida;</li>
              <li>respetar la normativa aplicable y las condiciones de los proveedores externos;</li>
              <li>comunicar con prontitud anomalías o sospechas de compromiso por el canal de asistencia disponible.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>12. Usos prohibidos</h2>
            <p>Queda prohibido utilizar TRAXION para:</p>
            <ul>
              <li>actividades ilícitas, fraudulentas o abusivas;</li>
              <li>eludir controles de seguridad, límites operativos o restricciones de acceso;</li>
              <li>interferir en el funcionamiento del servicio o intentar accesos no autorizados;</li>
              <li>utilizar credenciales o cuentas de terceros sin autorización;</li>
              <li>copiar o explotar software, contenidos o sistemas más allá de lo permitido por la ley o por estos Términos.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>13. Prueba gratuita</h2>
            <p>
              La configuración actual prevé una prueba de 14 días, activada con el primer acceso y sin tarjeta de
              pago. Durante la prueba pueden aplicarse límites específicos de cartera, posiciones, nominal por
              operación, multiplicador u otras funcionalidades, mostrados en la aplicación web y en la página
              comercial.
            </p>
            <p>
              La prueba está destinada a evaluar el servicio y puede limitarse a una sola activación por usuario o
              wallet, salvo que una promoción indique expresamente otra cosa.
            </p>
          </section>

          <section className={styles.section}>
            <h2>14. Planes de pago y precios</h2>
            <p>
              TRAXION puede ofrecer planes mensuales y anuales con límites y funciones diferentes. El precio,
              moneda, periodicidad, descuentos personales, capacidad del plan y condiciones adicionales aplicables
              serán los que se muestren al usuario antes de confirmar la compra.
            </p>
            <p>
              En caso de actualización del catálogo, los importes mostrados en el checkout son los aplicables a la
              contratación que el usuario vaya a confirmar. Los impuestos que correspondan se gestionarán de
              acuerdo con la normativa y con la información ofrecida durante el proceso de pago.
            </p>
          </section>

          <section className={styles.section}>
            <h2>15. Pagos mediante Stripe</h2>
            <p>
              Los pagos y las suscripciones pueden gestionarse mediante Stripe. TRAXION no debe almacenar los datos
              completos de la tarjeta. El usuario puede ser redirigido a interfaces de Stripe para completar el pago
              o gestionar la suscripción.
            </p>
            <p>
              La activación de funciones de pago depende de la confirmación técnica del estado de la suscripción. Un
              pago rechazado, vencido o irregular puede provocar la limitación o suspensión de las funciones de pago
              hasta que se regularice la situación.
            </p>
          </section>

          <section className={styles.section}>
            <h2>16. Renovación y cancelación de la suscripción</h2>
            <p>
              Salvo que el checkout indique otra cosa, los planes configurados como suscripciones recurrentes se
              renuevan por periodos sucesivos de la misma duración hasta su cancelación. La suscripción se gestiona
              a través del portal de facturación puesto a disposición por el servicio y Stripe.
            </p>
            <p>
              La fecha efectiva de finalización, el posible acceso hasta el término del periodo ya pagado y las
              consecuencias económicas de la cancelación serán las mostradas en el portal y las derivadas de la
              configuración aplicable, sin perjuicio de los derechos imperativos del consumidor.
            </p>
          </section>

          <section className={styles.section}>
            <h2>17. Derecho de desistimiento de los consumidores</h2>
            <p>
              El consumidor que celebre a distancia un contrato de pago dispone, salvo las excepciones previstas
              legalmente, de 14 días naturales desde la celebración del contrato para ejercer su derecho de
              desistimiento sin necesidad de indicar motivo alguno.
            </p>
            <p>
              Si el consumidor solicita expresamente que la prestación comience durante el plazo de desistimiento y
              después desiste, podrá deber el importe proporcional al servicio efectivamente prestado hasta la
              comunicación del desistimiento cuando concurran las condiciones previstas por la normativa. TRAXION no
              considera el simple uso del servicio como renuncia automática al derecho de desistimiento.
            </p>
            <p>
              El derecho podrá extinguirse en los casos permitidos por la ley cuando el servicio se haya ejecutado
              completamente tras el consentimiento expreso previo del consumidor y su reconocimiento expreso de que
              pierde el derecho. Cualquier consentimiento o reconocimiento necesario deberá recabarse de forma
              separada durante el proceso de contratación.
            </p>
            <p>
              El canal específico para ejercer el desistimiento está en proceso de completarse y se publicará antes
              de la puesta en servicio comercial definitiva. Una vez publicados los datos del Proveedor, el
              consumidor podrá utilizar igualmente cualquier declaración inequívoca admitida por la ley.
            </p>
            <h3>Modelo de formulario de desistimiento</h3>
            <p>
              Este modelo puede utilizarse si el consumidor desea desistir del contrato. No es obligatorio utilizar
              exactamente este formulario si la declaración enviada es inequívoca.
            </p>
            <p><strong>Destinatario:</strong> Proveedor de TRAXION — datos identificativos, domicilio profesional y canal de contacto en proceso de completarse.</p>
            <p>
              Por la presente comunico/comunicamos que desisto/desistimos de mi/nuestro contrato de prestación del
              servicio TRAXION contratado a distancia.
            </p>
            <ul>
              <li>Fecha de contratación: ____________________</li>
              <li>Nombre del/de los consumidor/es: ____________________</li>
              <li>Domicilio del/de los consumidor/es: ____________________</li>
              <li>Wallet o identificador de cuenta, si resulta necesario para localizar el contrato: ____________________</li>
              <li>Fecha de la comunicación: ____________________</li>
              <li>Firma del/de los consumidor/es, únicamente si el formulario se presenta en papel: ____________________</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>18. Reembolsos</h2>
            <p>
              Los reembolsos debidos como consecuencia de un ejercicio válido del derecho de desistimiento o de
              otros derechos imperativos se efectuarán conforme a la normativa aplicable. Fuera de esos supuestos y
              de condiciones promocionales expresas, los pagos relativos a periodos de servicio ya iniciados no
              generan automáticamente derecho a reembolso.
            </p>
          </section>

          <section className={styles.section}>
            <h2>19. Derechos del consumidor sobre servicios digitales</h2>
            <p>
              Nada en estos Términos limita los derechos imperativos del consumidor relativos a conformidad,
              suministro y remedios previstos por la normativa aplicable a los servicios digitales. En caso de falta
              de conformidad imputable al servicio, el consumidor conserva los remedios previstos legalmente.
            </p>
          </section>

          <section className={styles.section}>
            <h2>20. Riesgos de trading y pérdida de capital</h2>
            <p>
              El trading de activos digitales y perpetuals implica un riesgo elevado. El apalancamiento,
              volatilidad, liquidaciones, slippage, cambios de liquidez, movimientos rápidos de mercado, funding,
              errores de precio, latencia u otros acontecimientos pueden producir pérdidas significativas, incluida
              la pérdida parcial o total del capital destinado a la operativa.
            </p>
            <p>
              Los controles de riesgo, automatización y reconciliación reducen algunas categorías de error operativo,
              pero no eliminan el riesgo de mercado ni garantizan que una orden se ejecute al precio, cantidad o
              momento deseados.
            </p>
          </section>

          <section className={styles.section}>
            <h2>21. Riesgos de Hyperliquid y servicios de terceros</h2>
            <p>
              TRAXION depende de infraestructuras y servicios externos, entre ellos Hyperliquid, blockchain, redes,
              wallets, proveedores RPC o API, Stripe, infraestructura de hosting y, para determinadas funciones,
              proveedores de IA. Interrupciones, cambios, limitaciones, errores o indisponibilidad de esos terceros
              pueden afectar al servicio.
            </p>
            <p>
              TRAXION es un proyecto independiente y no está afiliado, aprobado ni patrocinado por Hyperliquid. El
              uso de servicios externos también queda sujeto a sus propios términos y condiciones.
            </p>
          </section>

          <section className={styles.section}>
            <h2>22. Disponibilidad, mantenimiento y cambios técnicos</h2>
            <p>
              El Proveedor puede realizar mantenimiento, actualizaciones, correcciones de seguridad y cambios
              necesarios para la evolución del servicio. No se garantiza un funcionamiento ininterrumpido o libre
              de errores.
            </p>
            <p>
              Cuando un cambio afecte materialmente a una suscripción de pago o a derechos del consumidor, se
              respetarán las obligaciones de información, continuidad y remedio previstas por la normativa aplicable.
            </p>
          </section>

          <section className={styles.section}>
            <h2>23. Suspensión y limitación del servicio</h2>
            <p>
              TRAXION puede suspender o limitar el acceso cuando resulte razonablemente necesario por motivos de
              seguridad, mantenimiento, prevención de abusos, incumplimiento de estos Términos, requerimientos
              legales, problemas de pago o protección de la integridad de la plataforma.
            </p>
            <p>
              Siempre que sea razonablemente posible y compatible con necesidades urgentes de seguridad, se
              informará al usuario de la suspensión y de las medidas disponibles para subsanar su causa.
            </p>
          </section>

          <section className={styles.section}>
            <h2>24. Limitación de responsabilidad</h2>
            <p>
              TRAXION responde de sus propias obligaciones dentro de los límites establecidos por la normativa
              aplicable. Ninguna cláusula excluye o limita responsabilidades que legalmente no puedan excluirse o
              limitarse, ni reduce los derechos imperativos reconocidos a los consumidores.
            </p>
            <p>
              En relaciones con profesionales y dentro de los límites permitidos por la ley, el Proveedor no será
              responsable de pérdidas indirectas o consecuenciales, lucro cesante, pérdida de oportunidades o daños
              derivados exclusivamente de decisiones de trading del usuario, condiciones de mercado o fallos de
              servicios externos fuera del control razonable del Proveedor.
            </p>
            <p>
              Estas limitaciones no se aplican cuando la ley prohíba limitarlas, incluidos los supuestos de dolo u
              otras responsabilidades imperativas previstas por el ordenamiento aplicable.
            </p>
          </section>

          <section className={styles.section}>
            <h2>25. Propiedad intelectual</h2>
            <p>
              El software, interfaces, marcas, textos, gráficos, arquitecturas, documentación y contenidos
              originales de TRAXION están protegidos por la normativa aplicable y pertenecen a sus respectivos
              titulares. Se concede al usuario una licencia personal, limitada, no exclusiva y revocable para
              utilizar el servicio conforme a estos Términos.
            </p>
          </section>

          <section className={styles.section}>
            <h2>26. Privacidad y datos personales</h2>
            <p>
              El tratamiento de datos personales se describe en la <a href="/privacy/">Política de Privacidad de TRAXION</a>,
              que contiene la información específica sobre datos tratados, finalidades, bases jurídicas, seguridad,
              destinatarios, conservación y derechos de los interesados.
            </p>
          </section>

          <section className={styles.section}>
            <h2>27. Contratación electrónica</h2>
            <p>
              Antes de la compra, el usuario debe poder consultar, almacenar y reproducir estos Términos. El proceso
              de compra muestra el plan, periodicidad y precio aplicables antes de confirmar el pago.
            </p>
            <p>
              Los datos introducidos en el checkout pueden revisarse y corregirse mediante los controles de la
              interfaz de pago antes de la confirmación. La versión de los Términos aceptada y los acontecimientos
              contractuales podrán conservarse electrónicamente con fines operativos, probatorios y legales.
            </p>
            <p>
              Al finalizar el procedimiento deberá proporcionarse una confirmación electrónica de la compra o
              activación. La lengua contractual será la de la versión de los Términos que se haya puesto a disposición
              del usuario y que este haya aceptado en el proceso de contratación. La versión en castellano permanece
              disponible para consumidores que contraten en España.
            </p>
          </section>

          <section className={styles.section}>
            <h2>28. Cambios en los Términos, planes y precios</h2>
            <p>
              El Proveedor puede actualizar estos Términos por razones legales, de seguridad, técnicas o comerciales.
              Los cambios materiales aplicables a contratos en curso se comunicarán con el preaviso exigido por la
              ley y no perjudicarán derechos imperativos ya adquiridos.
            </p>
            <p>
              Los cambios de precio para futuras renovaciones se aplicarán conforme a la información comunicada
              antes de la renovación y respetando la normativa aplicable.
            </p>
          </section>

          <section className={styles.section}>
            <h2>29. Ley aplicable y controversias</h2>
            <p>
              Estos Términos se rigen por la ley española, sin perjuicio de los derechos y normas imperativas que
              protejan al consumidor en su país de residencia habitual cuando resulten aplicables.
            </p>
            <p>
              Para consumidores, la competencia territorial será la establecida por las normas imperativas y estos
              Términos no imponen un fuero distinto del legalmente protegido. Para profesionales, salvo norma
              imperativa en contrario, cualquier fuero convencional se indicará junto con los datos legales
              definitivos del Proveedor.
            </p>
          </section>

          <section className={styles.section}>
            <h2>30. Reclamaciones y resolución de conflictos</h2>
            <p>
              Se invita al usuario a contactar con el Proveedor para facilitar la gestión directa de cualquier
              reclamación. Los consumidores conservan el derecho de acudir a las autoridades y organismos de
              resolución alternativa de conflictos que resulten competentes cuando lo prevea la normativa aplicable.
            </p>
            <p>
              El canal contractual dedicado está en proceso de completarse y se publicará antes de la puesta en
              servicio comercial definitiva.
            </p>
          </section>

          <section className={styles.section}>
            <h2>31. Disposiciones finales</h2>
            <p>
              Si alguna disposición de estos Términos se declara inválida o ineficaz, las restantes seguirán siendo
              aplicables en la medida permitida por la ley. La falta de ejercicio de un derecho no constituye
              renuncia al mismo.
            </p>
            <p>
              El usuario no podrá transferir su cuenta o credenciales a terceros infringiendo estos Términos.
              Cualquier transmisión del contrato por parte del Proveedor se realizará respetando la normativa
              aplicable y sin reducir los derechos imperativos del consumidor.
            </p>
          </section>
        </div>

        <div className={styles.footer}>
          Última actualización: 3 de septiembre de 2026. Los datos identificativos y el contacto del Proveedor se
          completarán antes de la puesta en servicio comercial definitiva.
        </div>
      </main>
    </div>
  );
}
